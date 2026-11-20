// The tab recorder produces independent VP8/Opus WebMs. Normalize their
// cluster timestamps to the PC capture clock and emit one WebM header.
// Keep whole keyframe-started segments for editing handles.
const SEGMENT = 0x18538067, INFO = 0x1549a966, TRACKS = 0x1654ae6b;
const CLUSTER = 0x1f43b675, TIMECODE = 0xe7, SCALE = 0x2ad7b1;
const LEVEL_ONE = new Set([INFO, TRACKS, CLUSTER, 0x1c53bb6b, 0x114d9b74, 0x1254c367, 0x1043a770, 0x1941a469]);

function vint(data, pos, id = false) {
  const first = data[pos];
  let mask = 128, size = 1;
  while (mask && !(first & mask)) { mask >>= 1; size++; }
  if (!mask || pos + size > data.length || (id && size > 4)) throw Error("Invalid WebM header");
  let value = id ? first : first - mask;
  let unknown = !id && value === mask - 1;
  for (let i = 1; i < size; i++) {
    value = value * 256 + data[pos+i];
    unknown = unknown && data[pos+i] === 255;
  }
  return {value: unknown ? Infinity : value, size};
}

function header(data, pos) {
  const id = vint(data, pos, true);
  const size = vint(data, pos + id.size);
  return {id: id.value, body: pos + id.size + size.size, size: size.value};
}

function* elements(data) {
  for (let pos = 0; pos < data.length;) {
    const {id, body, size} = header(data, pos);
    let end = body + size;
    if (!Number.isFinite(size)) {
      end = data.length;
      if (id === CLUSTER) {
        end = body;
        while (end < data.length) {
          const child = header(data, end);
          if (LEVEL_ONE.has(child.id)) break;
          if (!Number.isFinite(child.size)) throw Error("Unsupported WebM cluster");
          end = child.body + child.size;
        }
      } else if (id !== SEGMENT) throw Error("Unsupported WebM element");
    }
    if (end > data.length) throw Error("Incomplete WebM segment");
    yield {id, raw:data.subarray(pos, end), data:data.subarray(body, end)};
    pos = end;
  }
}

function uint(value) {
  if (!Number.isSafeInteger(value) || value < 0) throw Error("Invalid WebM timestamp or size");
  const bytes = [];
  do { bytes.unshift(value % 256); value = Math.floor(value / 256); } while (value);
  return Uint8Array.from(bytes);
}

const number = data => data.reduce((n, byte) => n * 256 + byte, 0);
function element(id, ...parts) {
  const payload = new Blob(parts);
  let size = 1;
  while (payload.size >= 2 ** (7 * size) - 1) size++;
  return new Blob([uint(id), uint(2 ** (7 * size) + payload.size), payload]);
}

const TRACK_ENTRY = 0xae, TRACK_NUMBER = 0xd7, VIDEO = 0xe0, AUDIO = 0xe1;
const TRACK_CONFIG = new Set([0x83, 0x86, 0x63a2, 0x56aa, 0x56bb, VIDEO, AUDIO]);
const byId = (a, b) => a.id - b.id;

function trackLayout(tracks) {
  return [...elements(tracks)].filter(el => el.id === TRACK_ENTRY).map(entry => {
    const fields = [...elements(entry.data)];
    const track = fields.find(el => el.id === TRACK_NUMBER);
    if (!track) throw Error("Missing WebM track number");
    // Ignore recorder-specific UIDs, labels and the order of EBML fields.
    const config = fields.filter(el => TRACK_CONFIG.has(el.id)).sort(byId).map(el => [
      el.id, el.id === VIDEO || el.id === AUDIO
        ? [...elements(el.data)].sort(byId).map(field => [field.id, ...field.data])
        : [...el.data]
    ]);
    return {number:number(track.data), config:JSON.stringify(config)};
  });
}

function matchTracks(layout, output) {
  if (layout.length !== output.length) return null;
  const unused = new Set(output);
  const changes = new Map();
  for (const track of layout) {
    const matches = [...unused].filter(old => old.config === track.config);
    const match = matches.find(old => old.number === track.number) ||
      (matches.length === 1 && matches[0]);
    if (!match) return null;
    unused.delete(match);
    if (track.number !== match.number) changes.set(track.number, match.number);
  }
  return changes;
}

function remapSample(sample, changes) {
  if (!changes.size) return sample.raw;
  if (sample.id === 0xa0) { // BlockGroup
    const fields = [...elements(sample.data)].filter(el => el.id !== 0xbf);
    return element(sample.id, ...fields.map(el => el.id === 0xa1 ? remapSample(el, changes) : el.raw));
  }
  const track = vint(sample.data, 0);
  const target = changes.get(track.value);
  if (target === undefined) return sample.raw;
  let width = 1;
  while (target >= 2 ** (7 * width) - 1) width++;
  return element(sample.id, uint(2 ** (7 * width) + target), sample.data.subarray(track.size));
}

// Chromium can flag an opaque VP8/VP9 track as having alpha. Some desktop
// decoders then wait for a BlockAdditional plane that never arrives. Clear
// only the flag payload, so cue positions, timestamps and codecs stay intact.
function unusedAlphaFlags(bytes) {
  const segment = [...elements(bytes)].find(el => el.id === SEGMENT);
  if (!segment) return [];
  const children = [...elements(segment.data)];
  const tracks = children.find(el => el.id === TRACKS);
  if (!tracks) return [];
  const entries = [...elements(tracks.data)];
  const hasChecksum = fields => fields.some(el => el.id === 0xbf);
  if (hasChecksum(children) || hasChecksum(entries)) return [];

  const flags = new Map();
  for (const entry of entries.filter(el => el.id === 0xae)) {
    const fields = [...elements(entry.data)];
    const video = fields.find(el => el.id === 0xe0);
    if (!video || hasChecksum(fields)) continue;
    const codec = fields.find(el => el.id === 0x86);
    if (!codec || !["V_VP8", "V_VP9"].includes(new TextDecoder().decode(codec.data))) continue;
    const settings = [...elements(video.data)];
    const flag = settings.find(el => el.id === 0x53c0);
    const track = fields.find(el => el.id === 0xd7);
    if (flag && track && number(flag.data) === 1 && !hasChecksum(settings)) {
      flags.set(number(track.data), flag.data);
    }
  }
  if (!flags.size) return [];

  for (const cluster of children.filter(el => el.id === CLUSTER)) {
    for (const el of elements(cluster.data)) {
      if (el.id !== 0xa0) continue; // SimpleBlock cannot carry an auxiliary plane.
      const group = [...elements(el.data)];
      if (!group.some(field => field.id === 0x75a1)) continue;
      const block = group.find(field => field.id === 0xa1);
      if (!block) return []; // ambiguous group; retain the original headers
      flags.delete(vint(block.data, 0).value);
    }
  }
  return [...flags.values()];
}

export async function downloadableWebm(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let flags;
  try {
    flags = unusedAlphaFlags(bytes);
  } catch {
    // Old or unfamiliar containers remain available as originally recorded.
    return file;
  }
  if (!flags.length) return file;
  const parts = [];
  let pos = 0;
  for (const flag of flags) {
    parts.push(file.slice(pos, flag.byteOffset), new Uint8Array(flag.byteLength));
    pos = flag.byteOffset + flag.byteLength;
  }
  return new Blob([...parts, file.slice(pos)], {type:"video/webm"});
}

function finishClip(group) {
  const duration = new DataView(new ArrayBuffer(8));
  duration.setFloat64(0, (group.end - group.start) * 1000000 / group.scale);
  const info = element(INFO, ...group.info, element(0x4489, duration.buffer));
  const segment = element(SEGMENT, info, group.tracks, ...group.clusters);
  return {start:group.start, end:group.end, blob:new Blob([group.head, segment], {type:"video/webm"})};
}

// One playable file per contiguous compatible layout. Each independent recorder
// starts with its own header and keyframe, so a split needs no re-encoding.
export async function assembleClips(parts) {
  const clips = [];
  let group;
  for (const part of parts) {
    const data = new Uint8Array(await part.file.arrayBuffer());
    const top = [...elements(data)];
    const head = top.find(el => el.id === 0x1a45dfa3);
    const segment = top.find(el => el.id === SEGMENT);
    if (!head || !segment) throw Error("Unplayable WebM segment");
    const children = [...elements(segment.data)];
    const info = children.find(el => el.id === INFO);
    const tracks = children.find(el => el.id === TRACKS);
    if (!info || !tracks) throw Error("WebM segment has no track information");
    const fields = [...elements(info.data)];
    const scale = number(fields.find(el => el.id === SCALE)?.data || []) || 1000000;
    const layout = trackLayout(tracks.data);
    let changes = group?.scale === scale ? matchTracks(layout, group.layout) : null;
    if (!changes) {
      if (group) clips.push(finishClip(group));
      group = {start:part.start, scale, layout, head:head.raw, tracks:tracks.raw, clusters:[],
        info:fields.filter(el => el.id !== 0x4489 && el.id !== 0xbf).map(el => el.raw)};
      changes = new Map();
    }
    group.end = part.end;
    const blocks = children.filter(el => el.id === CLUSTER);
    if (!blocks.length) throw Error("WebM segment has no media data");
    let firstTime;
    for (const block of blocks) {
      const fields = [...elements(block.data)];
      const stamp = fields.find(el => el.id === TIMECODE);
      if (!stamp) throw Error("Missing WebM timecode");
      const local = number(stamp.data);
      firstTime ??= local;
      const time = Math.round((part.start - group.start) * 1000000 / scale) + local - firstTime;
      const samples = fields.filter(el => el.id === 0xa3 || el.id === 0xa0).map(el => remapSample(el, changes));
      group.clusters.push(element(CLUSTER, element(TIMECODE, uint(time)), ...samples));
    }
  }
  if (group) clips.push(finishClip(group));
  return clips;
}

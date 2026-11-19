// Each rolling recording starts with its own WebM header and keyframe.
// Promoting a complete recording keeps its audio, video and pre-event footage playable.
const ROTATE_MS = 4000;
const POST_MS = 3000;
const extension = ".webm";
const clipPattern = /^(\d+)_([\w-]+)_([\w-]+)_[\w-]+\.webm$/;

export async function clipDirectory() {
  const root = await navigator.storage.getDirectory();
  return root.getDirectoryHandle("encore-tab-clips", {create: true});
}

export async function savedClips(dir) {
  const result = [];
  for await (const [name, handle] of dir.entries()) {
    const match = name.match(clipPattern);
    if (match && handle.kind === "file") {
      result.push({handle, time: Number(match[1]), id: match[2], reaction: match[3]});
    }
  }
  return result.sort((a, b) => b.time - a.time);
}

class Recording {
  constructor(stream, dir, mimeType, onError) {
    this.dir = dir;
    this.name = "buffer-" + crypto.randomUUID();
    this.start = performance.now();
    this.recorder = new MediaRecorder(stream, {mimeType, videoBitsPerSecond: 1800000});
    this.stopped = new Promise(resolve => this.recorder.addEventListener("stop", resolve, {once:true}));
    this.recorder.start(500);
    const fail = error => {
      if (!this.error) {
        this.error = error;
        onError(error);
      }
    };
    this.writes = dir.getFileHandle(this.name, {create:true}).then(async handle => {
      this.handle = handle;
      this.writer = await handle.createWritable();
    }).catch(fail);
    this.recorder.ondataavailable = ({data}) => {
      if (data.size) this.writes = this.writes.then(() => {
        if (!this.error) return this.writer.write(data);
      }).catch(fail);
    };
    this.recorder.onerror = event => fail(event.error || Error("탭 녹화 실패"));
  }

  async finish(filename, preview = false) {
    if (this.recorder.state !== "inactive") this.recorder.stop();
    await this.stopped; // the final dataavailable has now queued its write
    await this.writes;
    let moved = false;
    try {
      if ((!filename && !preview) || this.error) {
        if (this.writer) await this.writer.abort();
        if (this.error) throw this.error;
        return;
      }
      await this.writer.close();
      const file = await this.handle.getFile();
      if (!file.size) throw Error("녹화된 영상이 비어 있습니다.");
      // The rolling file is removed below. The short preview owns its bytes.
      if (preview) return new Blob([await file.arrayBuffer()], {type: this.recorder.mimeType});
      await this.handle.move(filename);
      moved = true;
      return this.handle;
    } finally {
      if (!moved && this.handle) await this.dir.removeEntry(this.name);
    }
  }
}

export class ClipBuffer {
  constructor(stream, dir, {onSignal, onPreview, onPreviewError, onSaved, onError}) {
    const mimeType = ["video/webm;codecs=vp8,opus", "video/webm"].find(
      type => MediaRecorder.isTypeSupported(type));
    if (!mimeType) throw Error("이 브라우저에서 WebM 녹화를 시작할 수 없습니다.");
    this.dir = dir;
    this.onSignal = onSignal;
    this.onPreview = onPreview;
    this.onPreviewError = onPreviewError || onError;
    this.onSaved = onSaved;
    this.onError = onError;
    this.newRecording = () => new Recording(stream, dir, mimeType, onError);
    this.warm = [this.newRecording()];
    this.jobs = new Set();
    this.revisions = new Map();
    this.rotation = setInterval(() => {
      try {
        this.warm.push(this.newRecording());
        if (this.warm.length > 2) this.track(this.warm.shift().finish());
      } catch (err) { this.onError(err); }
    }, ROTATE_MS);
  }

  track(promise) {
    const job = promise.catch(this.onError);
    this.jobs.add(job);
    job.finally(() => this.jobs.delete(job));
  }

  receive(e) {
    if (this.closed || e.revision <= (this.revisions.get(e.event_id) || 0)) return;
    this.revisions.set(e.event_id, e.revision);
    if (e.phase === "OPEN") {
      clearTimeout(this.post);
      if (!this.active) {
        // The oldest warm recorder covers up to two rotations, including inference delay.
        const recording = this.warm.shift() || this.newRecording();
        this.active = {recording, time: Date.now(), id:e.event_id, reaction:e.reaction.type,
          open: new Set()};
        if (this.onPreview) {
          const active = this.active;
          this.preview = this.warm.pop() || this.newRecording();
          this.previewTimer = setTimeout(() => {
            const preview = this.preview;
            this.preview = null;
            this.track(preview.finish(null, true).then(blob => {
              if (this.active === active && active.open.size) this.onPreview(blob, active.id);
            }).catch(this.onPreviewError));
          }, 2500);
        } else if (!this.warm.length) this.warm.push(this.newRecording());
      }
      this.active.open.add(e.event_id);
      this.onSignal("OPEN", e);
    } else if (this.active?.open.has(e.event_id)) {
      this.onSignal(e.phase, e);
      if (e.phase === "FINAL") {
        this.active.open.delete(e.event_id);
        if (!this.active.open.size) {
          const end = e.capture?.end_ms ?? performance.now();
          this.post = setTimeout(() => this.save(), Math.max(0, end + POST_MS - performance.now()));
        }
      }
    }
  }

  save() {
    clearTimeout(this.post);
    if (!this.active) return;
    const {recording, time, id, reaction} = this.active;
    clearTimeout(this.previewTimer);
    if (this.preview) this.track(this.preview.finish());
    this.preview = null;
    this.active = null;
    const filename = [time, id, reaction, crypto.randomUUID()].join("_") + extension;
    this.track(recording.finish(filename).then(handle => this.onSaved({handle,time,id,reaction})));
  }

  async stop() {
    this.closed = true;
    clearInterval(this.rotation);
    this.save();
    for (const recording of this.warm.splice(0)) this.track(recording.finish());
    await Promise.all(this.jobs);
  }
}

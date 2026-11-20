import {assemble} from "./webm.js";
import {ClipPlan, SEGMENT_MS, HANDLE_MS, RETENTION_MS, overlaps, missing} from "./plan.js";

async function verifyPlayback(blob) {
  const video = document.createElement("video");
  const url = URL.createObjectURL(blob);
  let timeout;
  try {
    await new Promise((resolve, reject) => {
      timeout = setTimeout(() => reject(Error("녹화 파일 디코딩 시간 초과")), 10000);
      video.onloadeddata = () => video.videoWidth > 0 ? resolve() : reject(Error("영상 트랙이 없습니다."));
      video.onerror = () => reject(Error("녹화 파일을 재생할 수 없습니다."));
      video.preload = "auto";
      video.src = url;
    });
  } finally {
    clearTimeout(timeout);
    video.onloadeddata = video.onerror = null;
    video.removeAttribute("src");
    video.load();
    URL.revokeObjectURL(url);
  }
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

// A ClipBuffer owns one explicit tab-share generation. The finalized segment
// index is shared by overlapping events; each pending interval acts as a pin.
export class ClipBuffer {
  constructor(stream, dir, {onSignal, onPreview, onPreviewError, onSaved, onError, generation = 1}) {
    const mimeType = ["video/webm;codecs=vp8,opus", "video/webm"].find(
      type => MediaRecorder.isTypeSupported(type));
    if (!mimeType) throw Error("WebM 녹화를 시작할 수 없습니다.");
    Object.assign(this, {dir, onSignal, onPreview, onSaved, onError, generation});
    this.onPreviewError = onPreviewError || onError;
    this.make = () => new Recording(stream, dir, mimeType, onError);
    this.current = this.make();
    this.segments = [];
    this.plans = new Map();
    this.queue = Promise.resolve();
    this.rotation = setInterval(() => {
      try { this.rotate(); } catch (err) { this.onError(err); }
    }, SEGMENT_MS);
  }

  get active() { return [...this.plans.values()].some(p => p.parts.some(c => !c.saved)); }

  enqueue(action) {
    this.queue = this.queue.then(action).catch(err => { if (!this.closed) this.onError(err); });
  }

  rotate() {
    if (this.closed) return;
    const previous = this.current;
    this.current = this.make();
    const start = previous.start, end = this.current.start;
    const done = previous.finish("ring-" + crypto.randomUUID()).then(handle => ({handle}), error => ({error}));
    this.enqueue(async () => {
      const {handle, error} = await done;
      if (error) throw error;
      this.segments.push({start, end, handle});
      if (!this.closed) await this.update(end);
    });
  }

  receive(e) {
    if (this.closed || e.generations?.camera !== this.generation || !e.capture?.triggers_ms?.length) return;
    let plan = this.plans.get(e.event_id);
    if (!plan) {
      plan = new ClipPlan(e.event_id, e.reaction.type);
      this.plans.set(e.event_id, plan);
    }
    if (!plan.apply(e)) return;
    this.onSignal(e.phase, e);
    if (e.phase === "OPEN" && this.onPreview && !this.preview) {
      this.preview = this.make();
      this.previewTimer = setTimeout(() => {
        const recording = this.preview;
        this.preview = null;
        const done = recording.finish(null, true).then(blob => {
          if (!this.closed && !plan.final) this.onPreview(blob, plan.id);
        }).catch(this.onPreviewError);
        this.enqueue(() => done);
      }, 2500);
    }
    this.enqueue(() => this.update(this.segments.at(-1)?.end ?? -Infinity));
  }

  async update(watermark) {
    if (this.closed) return;
    for (const plan of this.plans.values()) {
      for (const part of plan.parts) {
        if (this.closed) return;
        if (!part.saved && part.sealed && watermark >= part.end + HANDLE_MS) {
          await this.save(plan, part);
          part.saved = true;
        }
      }
    }
    const expired = this.segments.filter(s => s.end <= watermark - RETENTION_MS &&
      ![...this.plans.values()].some(p => p.parts.some(c => !c.saved && overlaps(s, c))));
    for (const s of expired) {
      await this.dir.removeEntry(s.handle.name);
      this.segments.splice(this.segments.indexOf(s), 1);
    }
  }

  async save(plan, part) {
    const available = this.segments.filter(s => overlaps(s, part));
    const gaps = missing(part.start, part.end, available);
    if (!available.some(s => s.end > part.start && s.start < part.end)) {
      this.onSignal("UNAVAILABLE", {event_id:plan.id, reaction:{type:plan.reaction}});
      return;
    }
    const inputs = [];
    for (const s of available) inputs.push({...s, file:await s.handle.getFile()});
    if (this.closed) return;
    const blob = await assemble(inputs);
    if (this.closed) return;
    await verifyPlayback(blob);
    if (this.closed) return;
    const id = part.index === 1 ? plan.id : plan.id + "-part-" + part.index;
    const time = Date.now();
    const filename = [time, id, plan.reaction, crypto.randomUUID()].join("_") + ".webm";
    const meta = {event_id:plan.id, part:part.index, camera_generation:this.generation,
      status:gaps.length ? "partial" : "ready", gaps, start_ms:part.start, end_ms:part.end,
      media_start_ms:available[0].start, media_end_ms:available.at(-1).end};
    const staging = "export-" + crypto.randomUUID();
    let committed = false;
    try {
      const handle = await this.dir.getFileHandle(staging, {create:true});
      const writer = await handle.createWritable();
      await writer.write(blob);
      await writer.close();
      if (this.closed) return;
      const sidecar = await this.dir.getFileHandle(filename+".json", {create:true});
      const index = await sidecar.createWritable();
      await index.write(JSON.stringify(meta));
      await index.close();
      if (this.closed) return;
      await handle.move(filename);
      if (this.closed) return;
      committed = true;
      // The OPFS move is the publication boundary. The UI can now open the file.
      part.saved = true;
      await this.onSaved({handle, time, id, reaction:plan.reaction, meta});
    } finally {
      if (!committed) for (const name of [staging, filename, filename+".json"]) {
        try { await this.dir.removeEntry(name); } catch (err) { if (err.name !== "NotFoundError") throw err; }
      }
    }
  }

  async stop() {
    this.closed = true;
    clearInterval(this.rotation);
    clearTimeout(this.previewTimer);
    const cleanup = [this.current.finish()];
    if (this.preview) cleanup.push(this.preview.finish());
    const results = await Promise.allSettled([...cleanup, this.queue]);
    try {
      await Promise.all(this.segments.splice(0).map(s => this.dir.removeEntry(s.handle.name)));
    } finally {
      this.plans.clear();
    }
    const failed = results.find(r => r.status === "rejected");
    if (failed) throw failed.reason;
  }
}

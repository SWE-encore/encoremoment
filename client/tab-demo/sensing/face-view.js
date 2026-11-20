// Positions are fractions of the shared tab. Try the full frame first, then
// overlapping closer views. Keep a successful view across brief occlusions.
const views = [
  [0.5, 0.5, 1, "전체 탭"],
  [0.5, 0.32, 0.35, "위쪽 중앙"],
  [0.5, 0.53, 0.45, "중앙"],
  [0.26, 0.36, 0.45, "왼쪽"],
  [0.74, 0.36, 0.45, "오른쪽"],
  [0.5, 0.72, 0.50, "아래쪽"],
  [0.25, 0.75, 0.50, "왼쪽 아래"],
  [0.75, 0.75, 0.50, "오른쪽 아래"]
];

export class FaceView {
  constructor() {
    this.index = 0;
    this.frames = 0;
    this.lastChange = 0;
    this.lastResult = 0;
    this.locked = false;
    this.misses = 0;
  }

  draw(ctx, video, frameNumber = this.frames + 1) {
    const [cx, cy, scale, name] = views[this.index];
    const sw = video.videoWidth * scale, sh = video.videoHeight * scale;
    ctx.drawImage(video, video.videoWidth * cx - sw / 2, video.videoHeight * cy - sh / 2,
                  sw, sh, 0, 0, ctx.canvas.width, ctx.canvas.height);
    this.frames = frameNumber;
    return name;
  }

  observe(frame, detected) {
    if (frame <= this.lastChange || frame <= this.lastResult) return;
    this.lastResult = frame;
    if (detected) {
      this.locked = true;
      this.misses = 0;
    } else if (++this.misses >= (this.locked ? 2 : 1)) {
      this.index = (this.index + 1) % views.length;
      this.lastChange = this.frames;
      this.misses = 0;
      this.locked = false;
    }
  }
}

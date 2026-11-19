// The AudioContext resamples the captured tab to 16 kHz.
class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    // First two packets complete the 15,600-sample YAMNet window.
    this.block = new Int16Array(7920);
    this.used = 0;
  }
  process(inputs) {
    const mono = inputs[0][0];
    if (!mono) return true;
    for (let i=0; i<mono.length; i++) {
      const value = mono[i];
      if (!this.used) this.startMs = Math.round((currentFrame+i)*1000/sampleRate);
      this.block[this.used++] = Math.round(Math.max(-1, Math.min(1, value)) * 32767);
      if (this.used === this.block.length) {
        this.port.postMessage({pcm:this.block.buffer, startMs:this.startMs}, [this.block.buffer]);
        this.block = new Int16Array(7680); // subsequent windows advance exactly one hop
        this.used = 0;
      }
    }
    return true;
  }
}
registerProcessor("encore-capture", Capture);

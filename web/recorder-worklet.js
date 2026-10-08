// Collects mono Float32 frames (AudioContext runs at 16 kHz) and posts ~100 ms chunks.
class Recorder extends AudioWorkletProcessor {
  constructor() { super(); this.buf = new Float32Array(1600); this.n = 0; }
  process(inputs) {
    const ch = inputs[0];
    if (!ch || !ch.length) return true;
    const L = ch[0], R = ch[1];
    for (let i = 0; i < L.length; i++) {
      this.buf[this.n++] = R ? (L[i] + R[i]) * 0.5 : L[i];
      if (this.n === this.buf.length) {
        this.port.postMessage(this.buf.slice(0));
        this.n = 0;
      }
    }
    return true;
  }
}
registerProcessor("recorder", Recorder);

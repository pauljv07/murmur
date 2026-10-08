// Runs in an AudioContext at 16 kHz. Input 0 = microphone, input 1 = computer audio (optional).
// Posts ~100 ms Float32 chunks: mono [mic...] or, with {stereo:true}, interleaved [mic, sys, ...].
class Recorder extends AudioWorkletProcessor {
  constructor(opts) {
    super();
    this.stereo = !!(opts.processorOptions && opts.processorOptions.stereo);
    this.frames = 1600;
    this.buf = new Float32Array(this.frames * (this.stereo ? 2 : 1));
    this.n = 0;
  }
  static mono(input, i) {
    if (!input || !input.length) return 0;
    let v = 0;
    for (const ch of input) v += ch[i];
    return v / input.length;
  }
  process(inputs) {
    const mic = inputs[0], sys = inputs[1];
    const len = (mic && mic[0] && mic[0].length) || (sys && sys[0] && sys[0].length) || 128;
    for (let i = 0; i < len; i++) {
      if (this.stereo) {
        this.buf[this.n * 2] = Recorder.mono(mic, i);
        this.buf[this.n * 2 + 1] = Recorder.mono(sys, i);
      } else {
        this.buf[this.n] = Recorder.mono(mic, i);
      }
      if (++this.n === this.frames) {
        this.port.postMessage(this.buf.slice(0));
        this.n = 0;
      }
    }
    return true;
  }
}
registerProcessor("recorder", Recorder);

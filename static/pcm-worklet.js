// Mono signed 16-bit little-endian PCM; use the AudioContext's actual sample rate.
class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = new DataView(new ArrayBuffer(4096 * 2));
    this.count = 0;
    this.active = true;
    this.port.onmessage = ({ data }) => {
      if (data === "stop") {
        this.active = false;
        this.flush();
        this.port.postMessage({ type: "stopped" });
      }
    };
  }

  flush() {
    if (!this.count) return;
    const buffer = this.samples.buffer.slice(0, this.count * 2);
    this.port.postMessage(buffer, [buffer]);
    this.count = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (this.active && channel) {
      for (const sample of channel) {
        const value = Math.max(-1, Math.min(1, sample));
        this.samples.setInt16(this.count * 2, value < 0 ? value * 32768 : value * 32767, true);
        if (++this.count === 4096) this.flush();
      }
    }
    return this.active;
  }
}

registerProcessor("pcm-recorder", PCMProcessor);

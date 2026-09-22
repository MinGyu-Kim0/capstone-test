// Run with Node's standard library: node tests/test_audio.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
let Processor;
const context = vm.createContext({
  AudioWorkletProcessor: class { constructor() { this.port = { postMessage: (data) => this.messages.push(data) }; this.messages = []; } },
  registerProcessor: (_, type) => { Processor = type; },
});
vm.runInContext(fs.readFileSync("static/pcm-worklet.js", "utf8"), context);
const processor = new Processor();
processor.process([[new Float32Array([-1, 0, 1, -2, 2, .5])]]);
assert.equal(processor.messages.length, 0);
processor.port.onmessage({ data: "stop" });
const pcm = new DataView(processor.messages[0]);
assert.deepEqual(Array.from({ length: 6 }, (_, i) => pcm.getInt16(i * 2, true)), [-32768, 0, 32767, -32768, 32767, 16383]);
assert.equal(processor.messages[1].type, "stopped");
assert.equal(processor.process([[new Float32Array([1])]]), false);
const buffered = new Processor();
buffered.process([[new Float32Array(4100).fill(.25)]]);
buffered.port.onmessage({ data: "stop" });
assert.equal(buffered.messages[0].byteLength, 8192);
assert.equal(buffered.messages[1].byteLength, 8);
assert.equal(buffered.messages[2].type, "stopped");
console.log("PASS: PCM clipping, little-endian encoding, chunking, final-buffer flush, and stop ordering");

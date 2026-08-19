export const PCM_SAMPLE_RATE = 16_000;
export const CHUNK_BYTES = 3_200;

export function floatToPcm16(samples) {
  const output = new Int16Array(samples.length);
  for (let index = 0; index < samples.length; index += 1) {
    const value = Math.max(-1, Math.min(1, samples[index]));
    output[index] = value < 0 ? Math.round(value * 32_768) : Math.round(value * 32_767);
  }
  return new Uint8Array(output.buffer);
}

export function splitPcmChunks(pcm, bytes = CHUNK_BYTES) {
  const chunks = [];
  for (let index = 0; index < pcm.length; index += bytes) chunks.push(pcm.slice(index, index + bytes));
  return chunks;
}

async function resampleMono(audioBuffer) {
  const frames = Math.ceil(audioBuffer.duration * PCM_SAMPLE_RATE);
  // 浏览器原生离线渲染会在下采样时进行适当的低通滤波；不能用线性插值，
  // 否则 48 kHz 等源音频下采样到 16 kHz 时会产生明显混叠。
  const offline = new OfflineAudioContext(1, frames, PCM_SAMPLE_RATE);
  const source = offline.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(offline.destination);
  source.start();
  const rendered = await offline.startRendering();
  return rendered.getChannelData(0);
}

export async function wavToPcm(file) {
  const context = new AudioContext();
  try {
    const audio = await context.decodeAudioData(await file.arrayBuffer());
    return floatToPcm16(await resampleMono(audio));
  } finally {
    await context.close();
  }
}

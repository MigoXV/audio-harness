const SAMPLE_RATE = 24_000;
const CHUNK_SAMPLES = 2_400;

function floatToPcm16(samples: Float32Array): Int16Array {
  const output = new Int16Array(samples.length);
  for (let index = 0; index < samples.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, samples[index]));
    output[index] = sample < 0 ? sample * 32768 : sample * 32767;
  }
  return output;
}

export function pcmToBase64(samples: Int16Array): string {
  const bytes = new Uint8Array(samples.buffer, samples.byteOffset, samples.byteLength);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

export interface MicrophoneCapture {
  stop: () => Promise<void>;
}

export async function startMicrophone(onChunk: (audio: string) => void): Promise<MicrophoneCapture> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  const context = new AudioContext();
  const source = context.createMediaStreamSource(stream);
  const processor = context.createScriptProcessor(4096, 1, 1);
  let pending: number[] = [];
  let position = 0;
  const step = context.sampleRate / SAMPLE_RATE;

  processor.onaudioprocess = (event) => {
    const input = event.inputBuffer.getChannelData(0);
    while (position + 1 < input.length) {
      const left = Math.floor(position);
      const fraction = position - left;
      pending.push(input[left] * (1 - fraction) + input[left + 1] * fraction);
      position += step;
    }
    position -= input.length;
    while (pending.length >= CHUNK_SAMPLES) {
      onChunk(pcmToBase64(floatToPcm16(Float32Array.from(pending.splice(0, CHUNK_SAMPLES)))));
    }
  };
  source.connect(processor);
  processor.connect(context.destination);
  return {
    stop: async () => {
      processor.onaudioprocess = null;
      source.disconnect();
      processor.disconnect();
      stream.getTracks().forEach((track) => track.stop());
      pending = [];
      await context.close();
    },
  };
}

export class PcmPlayer {
  private context: AudioContext | null = null;
  private nextTime = 0;
  private sources = new Set<AudioBufferSourceNode>();

  async push(base64: string): Promise<void> {
    this.context ??= new AudioContext({ sampleRate: SAMPLE_RATE });
    if (this.context.state === "suspended") await this.context.resume();
    const binary = atob(base64);
    const samples = new Float32Array(Math.floor(binary.length / 2));
    for (let index = 0; index < samples.length; index += 1) {
      const value = binary.charCodeAt(index * 2) | (binary.charCodeAt(index * 2 + 1) << 8);
      samples[index] = (value >= 0x8000 ? value - 0x10000 : value) / 32768;
    }
    const buffer = this.context.createBuffer(1, samples.length, SAMPLE_RATE);
    buffer.copyToChannel(samples, 0);
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    this.nextTime = Math.max(this.nextTime, this.context.currentTime);
    source.start(this.nextTime);
    this.nextTime += buffer.duration;
    this.sources.add(source);
    source.onended = () => this.sources.delete(source);
  }

  clear(): void {
    this.sources.forEach((source) => {
      try { source.stop(); } catch { /* already stopped */ }
    });
    this.sources.clear();
    this.nextTime = this.context?.currentTime ?? 0;
  }

  async close(): Promise<void> {
    this.clear();
    await this.context?.close();
    this.context = null;
  }
}

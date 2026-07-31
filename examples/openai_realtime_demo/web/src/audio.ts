const OUTPUT_SAMPLE_RATE = 24_000;
const CHUNK_SAMPLES = 2_400;
const VOICE_FRAME_SAMPLES = 240;
const VOICE_RMS_THRESHOLD = 0.012;

export function floatToPcm16(samples: Float32Array): Int16Array {
  const output = new Int16Array(samples.length);
  for (let index = 0; index < samples.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, samples[index]));
    output[index] = sample < 0 ? sample * 32768 : sample * 32767;
  }
  return output;
}

export function pcm16ToBase64(samples: Int16Array): string {
  const bytes = new Uint8Array(
    samples.buffer,
    samples.byteOffset,
    samples.byteLength,
  );
  let binary = "";
  const stride = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += stride) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + stride));
  }
  return btoa(binary);
}

export function lastVoicedOffsetMilliseconds(
  audio: string,
): number | undefined {
  const binary = atob(audio);
  const samples = Math.floor(binary.length / 2);
  let lastVoicedSample: number | undefined;

  for (let offset = 0; offset < samples; offset += VOICE_FRAME_SAMPLES) {
    const end = Math.min(samples, offset + VOICE_FRAME_SAMPLES);
    let sumSquares = 0;
    for (let index = offset; index < end; index += 1) {
      const low = binary.charCodeAt(index * 2);
      const high = binary.charCodeAt(index * 2 + 1);
      const unsigned = low | (high << 8);
      const signed = unsigned >= 0x8000 ? unsigned - 0x10000 : unsigned;
      const normalized = signed / 32768;
      sumSquares += normalized * normalized;
    }
    const rms = Math.sqrt(sumSquares / (end - offset));
    if (rms >= VOICE_RMS_THRESHOLD) {
      lastVoicedSample = end;
    }
  }

  return lastVoicedSample === undefined
    ? undefined
    : (lastVoicedSample / OUTPUT_SAMPLE_RATE) * 1_000;
}

export function silenceBase64(milliseconds = 3_000): string[] {
  const totalSamples = Math.round(
    (OUTPUT_SAMPLE_RATE * milliseconds) / 1_000,
  );
  const chunks: string[] = [];
  for (let offset = 0; offset < totalSamples; offset += CHUNK_SAMPLES) {
    chunks.push(
      pcm16ToBase64(
        new Int16Array(
          Math.min(CHUNK_SAMPLES, totalSamples - offset),
        ),
      ),
    );
  }
  return chunks;
}

export class StreamingResampler {
  private readonly step: number;
  private buffer = new Float32Array(0);
  private position = 0;

  constructor(
    inputSampleRate: number,
    outputSampleRate = OUTPUT_SAMPLE_RATE,
  ) {
    if (inputSampleRate <= 0 || outputSampleRate <= 0) {
      throw new Error("采样率必须大于零");
    }
    this.step = inputSampleRate / outputSampleRate;
  }

  push(input: Float32Array): Float32Array {
    if (input.length === 0) {
      return new Float32Array(0);
    }
    const merged = new Float32Array(this.buffer.length + input.length);
    merged.set(this.buffer);
    merged.set(input, this.buffer.length);

    const result: number[] = [];
    while (this.position + 1 < merged.length) {
      const left = Math.floor(this.position);
      const fraction = this.position - left;
      result.push(
        merged[left] * (1 - fraction) + merged[left + 1] * fraction,
      );
      this.position += this.step;
    }

    const consumed = Math.floor(this.position);
    this.buffer = merged.slice(consumed);
    this.position -= consumed;
    return Float32Array.from(result);
  }
}

class PcmChunker {
  private pending: number[] = [];

  constructor(private readonly onChunk: (chunk: Int16Array) => void) {}

  push(samples: Float32Array): void {
    for (const sample of samples) {
      this.pending.push(sample);
    }
    while (this.pending.length >= CHUNK_SAMPLES) {
      const chunk = Float32Array.from(
        this.pending.splice(0, CHUNK_SAMPLES),
      );
      this.onChunk(floatToPcm16(chunk));
    }
  }

  flush(): void {
    if (this.pending.length > 0) {
      this.onChunk(floatToPcm16(Float32Array.from(this.pending)));
      this.pending = [];
    }
  }
}

export interface MicrophoneCapture {
  stop: () => Promise<void>;
}

export async function startMicrophone(
  onChunk: (audio: string) => void,
): Promise<MicrophoneCapture> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });
  const context = new AudioContext({ sampleRate: OUTPUT_SAMPLE_RATE });
  const source = context.createMediaStreamSource(stream);
  const processor = context.createScriptProcessor(4_096, 1, 1);
  const resampler = new StreamingResampler(context.sampleRate);
  const chunker = new PcmChunker((chunk) => {
    onChunk(pcm16ToBase64(chunk));
  });

  processor.onaudioprocess = (event) => {
    chunker.push(
      resampler.push(event.inputBuffer.getChannelData(0)),
    );
  };
  source.connect(processor);
  processor.connect(context.destination);

  return {
    stop: async () => {
      processor.onaudioprocess = null;
      source.disconnect();
      processor.disconnect();
      for (const track of stream.getTracks()) {
        track.stop();
      }
      chunker.flush();
      await context.close();
    },
  };
}

export async function decodeAudioToPcm(
  source: ArrayBuffer,
): Promise<Int16Array> {
  const decoder = new AudioContext();
  try {
    const decoded = await decoder.decodeAudioData(source.slice(0));
    const frameCount = Math.ceil(decoded.duration * OUTPUT_SAMPLE_RATE);
    const offline = new OfflineAudioContext(
      1,
      frameCount,
      OUTPUT_SAMPLE_RATE,
    );
    const node = offline.createBufferSource();
    node.buffer = decoded;
    node.connect(offline.destination);
    node.start();
    const rendered = await offline.startRendering();
    return floatToPcm16(rendered.getChannelData(0));
  } finally {
    await decoder.close();
  }
}

export function splitPcmChunks(audio: Int16Array): Int16Array[] {
  const chunks: Int16Array[] = [];
  for (let offset = 0; offset < audio.length; offset += CHUNK_SAMPLES) {
    chunks.push(audio.slice(offset, offset + CHUNK_SAMPLES));
  }
  return chunks;
}

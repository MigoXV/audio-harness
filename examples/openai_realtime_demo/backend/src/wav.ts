const RIFF_HEADER_BYTES = 12;
const CHUNK_HEADER_BYTES = 8;

interface WavChunk {
  id: string;
  sizeOffset: number;
  dataOffset: number;
  size: number;
}

function findChunks(wav: Buffer): WavChunk[] {
  if (
    wav.length < RIFF_HEADER_BYTES ||
    wav.toString("ascii", 0, 4) !== "RIFF" ||
    wav.toString("ascii", 8, 12) !== "WAVE"
  ) {
    throw new Error("测试音频必须是 RIFF/WAVE 文件");
  }
  const chunks: WavChunk[] = [];
  let offset = RIFF_HEADER_BYTES;
  while (offset + CHUNK_HEADER_BYTES <= wav.length) {
    const id = wav.toString("ascii", offset, offset + 4);
    const size = wav.readUInt32LE(offset + 4);
    const dataOffset = offset + CHUNK_HEADER_BYTES;
    if (dataOffset + size > wav.length) {
      throw new Error(`WAV ${id} 数据块越界`);
    }
    chunks.push({
      id,
      sizeOffset: offset + 4,
      dataOffset,
      size,
    });
    offset = dataOffset + size + (size % 2);
  }
  return chunks;
}

export function wavExcerpt(
  wav: Buffer,
  seconds = 30,
): Buffer {
  const chunks = findChunks(wav);
  const format = chunks.find((chunk) => chunk.id === "fmt ");
  const data = chunks.find((chunk) => chunk.id === "data");
  if (!format || format.size < 16 || !data) {
    throw new Error("WAV 缺少 fmt 或 data 数据块");
  }
  const byteRate = wav.readUInt32LE(format.dataOffset + 8);
  const blockAlign = wav.readUInt16LE(format.dataOffset + 12);
  if (byteRate <= 0 || blockAlign <= 0) {
    throw new Error("WAV 采样参数不合法");
  }
  const requested = Math.floor((byteRate * seconds) / blockAlign) *
    blockAlign;
  const excerptSize = Math.min(data.size, requested);
  const outputEnd = data.dataOffset + excerptSize;
  const output = Buffer.from(wav.subarray(0, outputEnd));
  output.writeUInt32LE(excerptSize, data.sizeOffset);
  output.writeUInt32LE(output.length - 8, 4);
  return output;
}

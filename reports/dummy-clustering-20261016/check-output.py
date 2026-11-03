import asyncio,sys,json
from pathlib import Path
sys.path.insert(0,'/root/repositories/qwen-asr/scripts')
from benchmark_ai_meeting import inputs,session
async def main():
 out=Path('/root/repositories/audio-harness/reports/dummy-clustering-20261016')
 for protocol in ['grpc','ws']:
  result=await session(inputs(Path('/root/repositories/qwen-asr/data-bin/rita.wav'),[])[0],10,protocol)
  (out/(protocol+'-result.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
  events=[e for e in result['events'] if 'cluster' in e['type']]
  print(json.dumps({'protocol':protocol,'error':result['error'],'transcript':result['transcript'],'cluster_events':events},ensure_ascii=False),flush=True)
  assert result['error'] is None and result['transcript'] and any(e['type'].endswith('.completed') for e in events)
asyncio.run(main())

"""生成或校验 aha-grpc 强类型协议；随后运行 run_grpcio_tools.sh。"""

import argparse
from audio_harness.serving.aha_grpc.schema import generate

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    generate(check=parser.parse_args().check)

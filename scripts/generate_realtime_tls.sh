#!/usr/bin/env bash
set -euo pipefail

output_dir="${1:-model-bin/realtime-tls}"
cert_file="${output_dir}/cert.pem"
key_file="${output_dir}/key.pem"

if [[ -e "${cert_file}" || -e "${key_file}" ]]; then
  echo "证书或私钥已存在，未覆盖：${output_dir}" >&2
  exit 1
fi

mkdir -p "${output_dir}"
openssl req \
  -x509 \
  -newkey rsa:3072 \
  -sha256 \
  -nodes \
  -days 365 \
  -keyout "${key_file}" \
  -out "${cert_file}" \
  -subj "/CN=localhost/O=Audio Harness Development" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
chmod 600 "${key_file}"

echo "已生成开发证书：${cert_file}"
echo "已生成开发私钥：${key_file}"

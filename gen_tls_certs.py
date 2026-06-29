#!/usr/bin/env python3
"""
生成自签名 TLS 证书（测试环境用）。
用法:
    python gen_tls_certs.py  [COMMON_NAME]

会在 ~/.hermes/certs/ 下创建:
    server.crt  +  server.key  （服务端证书+私钥）
    ca.crt                          （CA 证书，供 Agent 校验用）

生产环境建议用 Let’s Encrypt 替换自签名证书。
"""
import os, sys, subprocess, datetime

CERT_DIR = os.path.expanduser("~/.hermes/certs")


def _run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[ERROR] {r.stderr.strip()}")
        sys.exit(1)
    return r


def generate(common_name: str = "hermes"):
    os.makedirs(CERT_DIR, exist_ok=True)
    key = os.path.join(CERT_DIR, "server.key")
    crt = os.path.join(CERT_DIR, "server.crt")
    ca  = os.path.join(CERT_DIR, "ca.crt")

    # 若已存在，询问覆盖
    if os.path.exists(crt):
        print(f"[WARN] {crt} 已存在，跳过生成（如需覆盖请删除后重试）")
        print(f"       {key}")
        print(f"       {ca}")
        return

    # 1) 生成 CA 私钥
    _run(f"openssl genrsa -out {CERT_DIR}/ca.key 2048")
    # 2) 生成 CA 自签名证书
    _run(
        f"openssl req -new -x509 -key {CERT_DIR}/ca.key "
        f'-subj "/CN=HermesCA/O=Hermes/CN=HermesCA" '
        f"-days 3650 -out {ca}"
    )
    # 3) 生成服务端私钥
    _run(f"openssl genrsa -out {key} 2048")
    # 4) 生成服务端 CSR
    _run(
        f"openssl req -new -key {key} "
        f'-subj "/CN={common_name}/O=Hermes" '
        f"-out {CERT_DIR}/server.csr"
    )
    # 5) 用 CA 签名服务端证书
    _run(
        f"openssl x509 -req -in {CERT_DIR}/server.csr "
        f"-CA {ca} -CAkey {CERT_DIR}/ca.key "
        f"-CAcreateserial -out {crt} -days 365"
    )

    # 清理中间文件
    for f in [f"{CERT_DIR}/ca.key", f"{CERT_DIR}/server.csr", f"{CERT_DIR}/ca.srl"]:
        if os.path.exists(f):
            os.remove(f)

    print("[OK] 证书已生成:")
    print(f"     {crt}")
    print(f"     {key}")
    print(f"     {ca}")
    print()
    print("配置环境变量启用 TLS:")
    print('     echo "TLS_ENABLED=1" >> ~/.hermes/.env')
    print(f'     echo "TLS_CERT_FILE={crt}" >> ~/.hermes/.env')
    print(f'     echo "TLS_KEY_FILE={key}" >> ~/.hermes/.env')
    print(f'     echo "TLS_CA_FILE={ca}" >> ~/.hermes/.env')


if __name__ == "__main__":
    cn = sys.argv[1] if len(sys.argv) > 1 else "hermes"
    generate(cn)

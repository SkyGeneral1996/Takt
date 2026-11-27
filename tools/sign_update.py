#!/usr/bin/env python3
"""Создание и подпись takt-update-1 для локальных файлов релиза."""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

# Позволяет запускать CLI как `python tools/sign_update.py` из любого каталога.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from takt.update import (
    MANIFEST_NAME, MANIFEST_SIG_NAME, MAX_ASSET_SIZE, UPDATE_FORMAT, UpdateError,
    _validate_https_url, _validate_asset_name, canonical_manifest,
    parse_version, validate_manifest,
)


def load_private_key(path: Path) -> Ed25519PrivateKey:
    raw = path.read_bytes()
    key = None
    for loader in (serialization.load_pem_private_key, serialization.load_der_private_key):
        try:
            key = loader(raw, password=None)
            break
        except (ValueError, TypeError, UnsupportedAlgorithm):
            continue
    if key is None and len(raw) == 32:
        key = Ed25519PrivateKey.from_private_bytes(raw)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Ожидается приватный ключ Ed25519")
    return key


def file_info(path: Path, kind: str) -> tuple[int, str]:
    _validate_asset_name(path.name, kind)
    size = 0
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_ASSET_SIZE:
                raise ValueError(f"Файл {path.name} превышает лимит размера")
            digest.update(chunk)
    if size == 0:
        raise ValueError(f"Файл {path.name} пуст")
    return size, digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", required=True, type=Path,
                        help="Путь к приватному ключу Ed25519 (PEM, DER или raw 32 байта)")
    parser.add_argument("--version", required=True, help="SemVer релиза, например 1.2.3")
    parser.add_argument("--tag", help="Tag релиза (по умолчанию v<version>)")
    parser.add_argument("--channel", default="stable", help="Канал из manifest")
    parser.add_argument("--base-url", required=True,
                        help="HTTPS-каталог, в котором будут опубликованы файлы")
    parser.add_argument("--output-dir", type=Path,
                        help="Каталог для manifest и .sig (по умолчанию рядом с первым файлом)")
    parser.add_argument("--asset", action="append", nargs=2, required=True,
                        metavar=("KIND", "PATH"),
                        help="Файл: kind windows-setup/windows-portable/android-apk/android-code и путь")
    return parser


def sign_release(args: argparse.Namespace) -> tuple[Path, Path]:
    try:
        parse_version(args.version)
    except ValueError as ex:
        raise ValueError(str(ex)) from ex
    version = args.version.strip()
    if version[:1] in ("v", "V"):
        version = version[1:]
    tag = args.tag or f"v{version}"
    try:
        parse_version(tag)
    except ValueError as ex:
        raise ValueError(str(ex)) from ex
    normalized_tag = tag.strip()[1:] if tag.strip()[:1] in ("v", "V") else tag.strip()
    if normalized_tag != version:
        raise ValueError("--tag не совпадает с --version")

    base_url = _validate_https_url(args.base_url).rstrip("/")
    parsed_base = urlsplit(base_url)
    if parsed_base.query or parsed_base.fragment:
        raise ValueError("--base-url должен быть каталогом без query и fragment")
    assets = []
    names = set()
    paths = []
    for kind, raw_path in args.asset:
        path = Path(raw_path).resolve()
        if not path.is_file():
            raise ValueError(f"Файл обновления не найден: {raw_path}")
        _validate_asset_name(path.name, kind)
        if path.name.casefold() in names:
            raise ValueError("Файлы обновления не должны иметь повторяющиеся имена")
        names.add(path.name.casefold())
        size, digest = file_info(path, kind)
        assets.append({
            "name": path.name,
            "url": f"{base_url}/{quote(path.name, safe='')}",
            "size": size,
            "sha256": digest,
            "kind": kind,
        })
        paths.append(path)
    manifest = validate_manifest({
        "format": UPDATE_FORMAT,
        "version": version,
        "channel": args.channel,
        "assets": assets,
    })
    output_dir = (args.output_dir or paths[0].parent).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / MANIFEST_NAME
    signature_path = output_dir / MANIFEST_SIG_NAME
    key = load_private_key(args.key)
    canonical = canonical_manifest(manifest)
    signature = key.sign(canonical)

    for destination, data in ((manifest_path, canonical), (signature_path, signature)):
        fd, temporary = tempfile_named(destination)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return manifest_path, signature_path


def tempfile_named(destination: Path) -> tuple[int, str]:
    import tempfile
    return tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        manifest_path, signature_path = sign_release(args)
    except (OSError, ValueError, UpdateError) as ex:
        parser.error(str(ex))
    print(f"Создан {manifest_path}")
    print(f"Создан {signature_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

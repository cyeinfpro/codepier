"""Optional Pi native input extension for real live image content, not a chat API.

Only generated, integrity-checked CodePier uploads bound to the current session can
be attached. Native config, auth, tools, approvals and other extensions are intact.
The source lives in a Python package member so existing Agent ZIP allowlists and
one-click update installers transport it without additional package exceptions.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
from shared.native_cli import identifier
from shared.util import atomic_json

BRIDGE_SOURCE = r'''import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';

function readRegular(file, maximum) {
  const fd = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
  try {
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.size > maximum) throw new Error('Invalid attachment file');
    return fs.readFileSync(fd);
  } finally { fs.closeSync(fd); }
}

function imageMime(raw) {
  if (raw.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))) return 'image/png';
  if (raw[0] === 255 && raw[1] === 216 && raw[2] === 255) return 'image/jpeg';
  if (['GIF87a','GIF89a'].includes(raw.subarray(0,6).toString())) return 'image/gif';
  if (raw.subarray(0,4).toString() === 'RIFF' && raw.subarray(8,12).toString() === 'WEBP') return 'image/webp';
  throw new Error('Unsupported or invalid image data');
}

export default function codepierNativeImageInput(pi) {
  pi.on('input', async (event, ctx) => {
    const expression = /\[\[(?:codepier|relay)-image:([a-f0-9]{32})\]\]/g;
    const matches = [...event.text.matchAll(expression)];
    if (!matches.length) return { action: 'continue' };
    try {
      if (matches.length > 10) throw new Error('At most ten live images per message');
      const manifestPath = process.env.CODEPIER_PI_IMAGE_MANIFEST || process.env.RELAY_PI_IMAGE_MANIFEST;
      if (!manifestPath) throw new Error('No CodePier image manifest for this process');
      const manifest = JSON.parse(readRegular(manifestPath, 65536).toString('utf8'));
      if (manifest.version !== 1 || !/^[a-f0-9]{32}$/.test(manifest.session_id)) throw new Error('Invalid manifest');
      const uploadRoot = path.resolve(path.dirname(manifestPath), '..', 'uploads');
      const attached = [...(event.images || [])];
      const labels = new Map();
      for (const match of matches) {
        const id = match[1]; if (labels.has(id)) continue;
        const item = manifest.files[id];
        if (!item || path.dirname(item.path) !== uploadRoot ||
            !new RegExp('^'+id+'\\.[a-z0-9]{1,8}$').test(path.basename(item.path)) ||
            fs.realpathSync(path.dirname(item.path)) !== uploadRoot) throw new Error('Image is not bound to this session');
        const raw = readRegular(item.path, 20 * 1024 * 1024);
        if (raw.length !== item.size || createHash('sha256').update(raw).digest('hex') !== item.sha256)
          throw new Error('Image changed after upload');
        attached.push({ type: 'image', data: raw.toString('base64'), mimeType: imageMime(raw) });
        labels.set(id, '[图片：'+item.name+']');
      }
      return { action: 'transform', text: event.text.replace(expression, (_,id) => labels.get(id)), images: attached };
    } catch (_error) {
      // Do not accidentally send marker text as if it were an attached image.
      ctx.ui?.notify('CodePier 图片未能附加：请检查附件是否仍存在并重新附加。未发送此消息。', 'error');
      return { action: 'handled' };
    }
  });
}
'''


SHELL_GUARD_SOURCE = r'''export default function codepierShellTimeout(pi) {
  pi.on('tool_call', (event) => {
    // Pi applies in-place input changes before invoking its native Bash tool.
    // Keep explicit deadlines and native validation; never replay a timed-out command.
    if (event.toolName === 'bash' && event.input.timeout === undefined) {
      event.input.timeout = 120;
    }
  });
}
'''


def prepare_shell_guard(directory: Path) -> Path:
    return _prepare_extension(directory, SHELL_GUARD_SOURCE, 'codepier-shell-timeout')


def prepare_extension(directory: Path) -> Path:
    return _prepare_extension(directory, BRIDGE_SOURCE, 'codepier-images')


def _prepare_extension(directory: Path, source: str, prefix: str) -> Path:
    folder = directory / 'extensions'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if folder.is_symlink():
        raise ValueError('Unsafe Pi extension directory')
    raw = source.encode()
    target = folder / (prefix + '-' + hashlib.sha256(raw).hexdigest()[:16] + '.mjs')
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except FileExistsError:
        if target.is_symlink() or target.read_bytes() != raw:
            raise ValueError('Pi extension integrity mismatch')
    else:
        with os.fdopen(fd, 'wb') as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    return target


def image_manifest(directory: Path, sid: str) -> Path:
    folder = directory / 'image-manifests'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if folder.is_symlink():
        raise ValueError('Unsafe image manifest directory')
    return folder / (identifier(sid) + '.json')


def sync_manifest(directory: Path, db, sid: str) -> Path:
    records = db.execute('SELECT a.* FROM attachments a JOIN session_files s ON s.file=a.id WHERE s.session=? AND a.ready=1', (sid,)).fetchall()
    files = {r['id']: {'path':r['path'],'name':r['name'],'size':r['size'],'sha256':r['sha']} for r in records}
    destination = image_manifest(directory,sid)
    if destination.is_symlink():
        raise ValueError('Unsafe image manifest path')
    atomic_json(destination, {'version':1,'session_id':sid,'files':files})
    return destination

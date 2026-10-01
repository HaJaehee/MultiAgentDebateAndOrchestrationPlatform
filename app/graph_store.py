"""그래프 토론의 그래프 파일 — `data/graphs/<id>.json`.

`conf.json` 에 두지 않는 이유: 편집기 좌표까지 들어가 설정 파일이 비대해지고, 사람이 손으로
고치는 파일과 화면이 매번 다시 쓰는 파일이 섞입니다. 파일로 두면 폐쇄망 배포본에도 그대로
실립니다.

대화는 그래프 **id** 를 고르고, 턴이 시작될 때 그 내용을 `sessions.graph_snapshot` 에 고정합니다
(`OrchestratorEngine._run_turn`). 그래서 토론 중에 파일을 고쳐도 도는 턴은 흔들리지 않고, 파일을
지워도 지난 턴의 기록은 그때의 그래프로 남습니다.
"""

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from app.config import DATA_DIR
from app.orchestration.graph import GraphSpec, parse_graph

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


def graphs_dir() -> Path:
    return DATA_DIR / "graphs"


def _path(graph_id: str, directory: Optional[Path] = None) -> Path:
    if not _ID_RE.match(graph_id or ""):
        raise ValueError(f"그래프 id 가 올바르지 않습니다: {graph_id!r}")
    return (directory or graphs_dir()) / f"{graph_id}.json"


def list_graphs(directory: Optional[Path] = None) -> List[Tuple[str, str]]:
    """(id, 이름) 목록, 이름순. 읽지 못하는 파일은 건너뛰고 로그에 남깁니다."""
    root = directory or graphs_dir()
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.glob("*.json")):
        try:
            spec = parse_graph(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            logger.warning(f"Skipping unreadable graph file {path}: {exc}")
            continue
        found.append((spec.id, spec.name or spec.id))
    return sorted(found, key=lambda item: item[1].casefold())


def load_graph(graph_id: str, directory: Optional[Path] = None) -> GraphSpec:
    """그래프를 읽습니다. 없으면 FileNotFoundError, 형식이 틀리면 ValueError."""
    path = _path(graph_id, directory)
    spec = parse_graph(json.loads(path.read_text(encoding="utf-8")))
    if spec.id != graph_id:
        raise ValueError(f"{path.name} 안의 id({spec.id})가 파일 이름과 다릅니다")
    return spec


def save_graph(spec: GraphSpec, directory: Optional[Path] = None) -> Path:
    """임시 파일에 다 쓴 뒤 바꿔 끼웁니다. 쓰는 도중에 멈춰도 반쯤 쓴 그래프가 남지 않습니다."""
    path = _path(spec.id, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(spec.dump(), ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{spec.id}.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def free_graph_id(base: str, directory: Optional[Path] = None) -> str:
    """`base`, `base-2`, `base-3` … 중 아직 없는 id."""
    stem = re.sub(r"[^A-Za-z0-9_\-]+", "-", base).strip("-")[:48] or "graph"
    if not stem[0].isalnum():
        stem = f"g{stem}"
    candidate, n = stem, 1
    while _path(candidate, directory).exists():
        n += 1
        candidate = f"{stem}-{n}"
    return candidate

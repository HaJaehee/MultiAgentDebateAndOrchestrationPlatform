"""SQL 은 문자열로 잇지 않는다 (DB 인젝션 방어).

방문자·사용자가 적은 값은 ORM 이나 바인딩 매개변수로만 DB 에 갑니다. 그래서 SQL 문장을 f-string·
`+`·`%`·`.format()` 으로 만드는 곳이 새로 생기면 실패합니다. 예외는 스키마 보강
(`app/database/session.py`) 하나뿐입니다 — 테이블·컬럼 이름은 바인딩 매개변수가 될 수 없어
문장에 그대로 넣고, 대신 이름과 컬럼 정의의 모양을 확인한 뒤에만 실행합니다.
"""

import ast
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from app.database import session as db_session

APP = Path(__file__).resolve().parents[1] / "app"

# SQL 문장을 받는 호출: `text(...)`, `conn.execute(...)`, `cursor.execute(...)`, `exec_driver_sql(...)`.
SQL_CALLS = {"text", "execute", "exec_driver_sql", "executescript", "executemany"}

# (파일, 함수) — 문자열로 SQL 을 만드는 것을 알고 둔 곳.
ALLOWED = {
    ("database/session.py", "_on_connect"),          # PRAGMA busy_timeout={정수 상수}
    ("database/session.py", "_add_missing_columns"),  # 식별자·컬럼 정의 확인 뒤 ALTER TABLE
}


def _built_string(node: ast.AST) -> bool:
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(part, ast.FormattedValue) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return True
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format")


def _call_name(node: ast.Call) -> Optional[str]:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


class _Finder(ast.NodeVisitor):
    """SQL 을 받는 호출에 만든 문자열이 들어가는 곳을, 그 호출을 감싼 가장 안쪽 함수 이름과 함께 모읍니다."""

    def __init__(self, rel: str):
        self.rel = rel
        self.stack: List[str] = []
        self.found: List[Tuple[str, str, int]] = []

    def _function(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def visit_Call(self, node: ast.Call) -> None:
        if _call_name(node) in SQL_CALLS and node.args and _built_string(node.args[0]):
            self.found.append((self.rel, self.stack[-1] if self.stack else "<module>", node.lineno))
        self.generic_visit(node)


def _string_built_sql() -> List[Tuple[str, str, int]]:
    found = []
    for path in APP.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        finder = _Finder(path.relative_to(APP).as_posix())
        finder.visit(ast.parse(path.read_text(encoding="utf-8")))
        found.extend(finder.found)
    return found


def test_sql_is_never_built_from_strings_outside_the_schema_backfill():
    unexpected = [hit for hit in _string_built_sql() if hit[:2] not in ALLOWED]
    assert unexpected == [], "SQL 은 ORM 이나 바인딩 매개변수로 — 문자열로 잇지 않습니다"


def test_the_allowed_places_still_exist():
    """고쳐서 사라진 예외가 목록에 남아 있으면, 나중에 같은 이름으로 생긴 것을 그냥 통과시킵니다."""
    assert {hit[:2] for hit in _string_built_sql()} == ALLOWED


def test_every_backfilled_column_passes_the_shape_check():
    for table, columns in db_session._ADDED_COLUMNS.items():
        assert db_session._identifier(table) == table
        for column, ddl in columns.items():
            assert db_session._identifier(column) == column
            assert db_session._column_ddl(ddl) == ddl


@pytest.mark.parametrize("name", ["messages; DROP TABLE sessions", "a b", "x--", "1abc", "", "名前"])
def test_identifiers_that_could_carry_sql_are_refused(name):
    with pytest.raises(ValueError):
        db_session._identifier(name)


@pytest.mark.parametrize("ddl", [
    "TEXT; DROP TABLE sessions",
    "TEXT -- comment",
    "TEXT DEFAULT 'a",
    "TEXT /* x */",
    'TEXT DEFAULT "x"',
])
def test_column_definitions_that_could_carry_sql_are_refused(ddl):
    with pytest.raises(ValueError):
        db_session._column_ddl(ddl)

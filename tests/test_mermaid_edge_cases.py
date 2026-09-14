"""Mermaid 정규화·검사의 엣지케이스 — 실제 파서의 판정을 기준표로 고정합니다.

각 항목은 NiceGUI 가 동봉한 mermaid 의 `mermaid.parse()` 로 **원문**과 **정규화본**을 각각
돌린 결과입니다 (v0.8.1 이후 엣지케이스 탐사, 121건). 브라우저 없이 네 가지를 지킵니다.

1. 거짓 양성 0 — 파서가 받는 것(정규화본 기준)을 린터가 지적하지 않는다.
2. 파서가 거부하는 것은 정규화가 고치거나 린터가 잡는다 (조용히 통과시키지 않는다).
3. 정규화가 만든 글자는 파서가 받은 **바로 그 글자**다 (스냅샷).
4. 정규화는 flowchart·mindmap 밖에서 글자를 바꾸지 않고, 여러 번 적용해도 같다.

탐사에서 드러나 고친 것: YAML 머리말·BOM 을 선언 없음으로 본 것, 비대칭 모양 `A>x]` 의
괄호 짝, `opt:::red` 를 시퀀스 문법으로 본 것, `end;` 를 닫힘으로 못 본 것, 노트 글의 백틱이
라벨을 깨뜨린 것, 시퀀스·상태·간트 등의 `[결제 (PG)]` 에 따옴표를 덧씌워 보이는 글자를
바꾼 것, 스타일의 `rgb()`, 원통·평행사변형·이중 원·subgraph 제목의 괄호, `subgraph 아이디 "제목"`.
"""

import pytest

from app.mermaid_lint import diagram_kind, format_issues, lint_mermaid
from app.orchestration.engine import normalize_mermaid

# 이름: (원문, 파서가 원문을 받았나, 파서가 정규화본을 받았나, 정규화본)
ORACLE = {
    'front-matter': (
        '---\ntitle: 결제 흐름\n---\nflowchart LR\n  A --> B',
        True, True,
        '---\ntitle: 결제 흐름\n---\nflowchart LR\n  A --> B',
    ),
    'front-matter-config': (
        '---\nconfig:\n  theme: forest\n---\ngraph TD\n  A --> B',
        True, True,
        '---\nconfig:\n  theme: forest\n---\ngraph TD\n  A --> B',
    ),
    'init-directive': (
        "%%{init: {'theme':'dark'}}%%\nflowchart LR\n  A --> B",
        True, True,
        "%%{init: {'theme':'dark'}}%%\nflowchart LR\n  A --> B",
    ),
    'flowchart-elk': (
        'flowchart-elk TD\n  A --> B',
        True, True,
        'flowchart-elk TD\n  A --> B',
    ),
    'graph-semicolon-header': (
        'graph TD;\n  A-->B;',
        True, True,
        'graph TD;\n  A-->B;',
    ),
    'leading-blank-and-bom': (
        '\ufeffflowchart LR\n  A --> B',
        True, True,
        'flowchart LR\n  A --> B',
    ),
    'mermaid-word-header': (
        'mermaid\nflowchart LR\n  A --> B',
        False, True,
        'flowchart LR\n  A --> B',
    ),
    'tabs-indent': (
        'flowchart LR\n\tA --> B\n\tsubgraph S\n\t\tB --> C\n\tend',
        True, True,
        'flowchart LR\n\tA --> B\n\tsubgraph S\n\t\tB --> C\n\tend',
    ),
    'crlf': (
        'flowchart LR\r\n  A --> B\r\n  Note right of A: x\r\n',
        False, True,
        'flowchart LR\n  A --> B\n  A -.- mado_note_1["x"]\n  classDef madoNote fill:#fef9c3,stroke:#ca8a04,color:#713f12\n  class mado_note_1 madoNote',
    ),
    'asymmetric-shape': (
        'flowchart LR\n  A>비대칭] --> B',
        True, True,
        'flowchart LR\n  A>비대칭] --> B',
    ),
    'asymmetric-shape-paren': (
        'flowchart LR\n  A>결제 (PG)] --> B',
        False, True,
        'flowchart LR\n  A>"결제 (PG)"] --> B',
    ),
    'trapezoid': (
        'flowchart LR\n  A[/사다리꼴\\] --> B[\\역사다리꼴/]',
        True, True,
        'flowchart LR\n  A[/사다리꼴\\] --> B[\\역사다리꼴/]',
    ),
    'triple-circle': (
        'flowchart LR\n  A(((삼중))) --> B',
        True, True,
        'flowchart LR\n  A(("(삼중)")) --> B',
    ),
    'cylinder-paren-inner': (
        'flowchart LR\n  A[(DB (주))] --> B',
        False, True,
        'flowchart LR\n  A[("DB (주)")] --> B',
    ),
    'stadium': (
        'flowchart LR\n  A([스타디움]) --> B',
        True, True,
        'flowchart LR\n  A([스타디움]) --> B',
    ),
    'new-shape-syntax': (
        'flowchart LR\n  A@{ shape: rect, label: "사각" } --> B',
        True, True,
        'flowchart LR\n  A@{ shape: rect, label: "사각" } --> B',
    ),
    'markdown-string': (
        'flowchart LR\n  A["`**굵게** (강조)`"] --> B',
        True, True,
        'flowchart LR\n  A["`**굵게** (강조)`"] --> B',
    ),
    'class-shorthand': (
        'flowchart LR\n  A:::red --> B\n  classDef red fill:#f00',
        True, True,
        'flowchart LR\n  A:::red --> B\n  classDef red fill:#f00',
    ),
    'kw-class-shorthand': (
        'flowchart LR\n  opt:::red --> B\n  classDef red fill:#f00',
        True, True,
        'flowchart LR\n  opt:::red --> B\n  classDef red fill:#f00',
    ),
    'loop-class-shorthand': (
        'flowchart LR\n  loop:::red --> B\n  classDef red fill:#f00',
        True, True,
        'flowchart LR\n  loop:::red --> B\n  classDef red fill:#f00',
    ),
    'end-in-label': (
        'flowchart LR\n  A[end user] --> B',
        True, True,
        'flowchart LR\n  A[end user] --> B',
    ),
    'end-in-label2': (
        'flowchart LR\n  A[the end] --> B',
        True, True,
        'flowchart LR\n  A[the end] --> B',
    ),
    'end-in-edge-label': (
        'flowchart LR\n  A -->|end| B',
        True, True,
        'flowchart LR\n  A -->|end| B',
    ),
    'end-semicolon': (
        'graph TD\n  subgraph S\n    A --> B\n  end;',
        True, True,
        'graph TD\n  subgraph S\n    A --> B\n  end;',
    ),
    'End-capital-node': (
        'flowchart LR\n  start --> End',
        True, True,
        'flowchart LR\n  start --> End',
    ),
    'end-quoted-label': (
        'flowchart LR\n  A["end"] --> B',
        True, True,
        'flowchart LR\n  A["end"] --> B',
    ),
    'endpoint-node': (
        'flowchart LR\n  A --> endpoint',
        True, True,
        'flowchart LR\n  A --> endpoint',
    ),
    'end-dash-node': (
        'flowchart LR\n  A --> end-node',
        False, False,
        'flowchart LR\n  A --> end-node',
    ),
    'subgraph-title-paren': (
        'graph TD\n  subgraph S[그룹 (A)]\n    A --> B\n  end',
        False, True,
        'graph TD\n  subgraph S["그룹 (A)"]\n    A --> B\n  end',
    ),
    'subgraph-title-paren-bare': (
        'graph TD\n  subgraph 그룹 (A)\n    A --> B\n  end',
        False, True,
        'graph TD\n  subgraph "그룹 (A)"\n    A --> B\n  end',
    ),
    'style-rgb': (
        'flowchart LR\n  A --> B\n  style A fill:rgb(255,0,0)',
        False, True,
        'flowchart LR\n  A --> B\n  style A fill:#ff0000',
    ),
    'classdef-rgba': (
        'flowchart LR\n  A --> B\n  classDef c fill:rgba(0,0,0,0.5)',
        False, True,
        'flowchart LR\n  A --> B\n  classDef c fill:#00000080',
    ),
    'click-callback': (
        'flowchart LR\n  A --> B\n  click A call cb("x")',
        True, True,
        'flowchart LR\n  A --> B\n  click A call cb("x")',
    ),
    'click-href-paren': (
        'flowchart LR\n  A --> B\n  click A "https://x.io/a_(b)" "도움말 (새 창)"',
        True, True,
        'flowchart LR\n  A --> B\n  click A "https://x.io/a_(b)" "도움말 (새 창)"',
    ),
    'dash-text-paren': (
        'flowchart LR\n  A -- 요청 (HTTP) --> B',
        True, True,
        'flowchart LR\n  A -- 요청 (HTTP) --> B',
    ),
    'edge-label-bracket-paren': (
        'flowchart LR\n  A -->|[x] (y)| B',
        False, False,
        'flowchart LR\n  A -->|[x] (y)| B',
    ),
    'label-pipe-paren': (
        'flowchart LR\n  A[a|b (c)] --> B',
        False, False,
        'flowchart LR\n  A[a|b (c)] --> B',
    ),
    'label-colon-paren': (
        'flowchart LR\n  A[주의: 결제 (PG)] --> B',
        False, True,
        'flowchart LR\n  A["주의: 결제 (PG)"] --> B',
    ),
    'label-hash-entity': (
        'flowchart LR\n  A["#quot;인용#quot;"] --> B',
        True, True,
        'flowchart LR\n  A["#quot;인용#quot;"] --> B',
    ),
    'two-paren-labels-one-line': (
        'flowchart LR\n  A[결제 (PG)] --> B[주문 (Order)]',
        False, True,
        'flowchart LR\n  A["결제 (PG)"] --> B["주문 (Order)"]',
    ),
    'paren-in-brace-quoted': (
        'flowchart LR\n  A{"결정 (Y/N)"} --> B',
        True, True,
        'flowchart LR\n  A{"결정 (Y/N)"} --> B',
    ),
    'round-node-quoted-paren': (
        'flowchart LR\n  A("결제 (PG)") --> B',
        True, True,
        'flowchart LR\n  A("결제 (PG)") --> B',
    ),
    'double-circle-quoted': (
        'flowchart LR\n  A(("원 (x)")) --> B',
        True, True,
        'flowchart LR\n  A(("원 (x)")) --> B',
    ),
    'edge-label-quoted-paren': (
        'flowchart LR\n  A -->|"결제 (PG)"| B',
        True, True,
        'flowchart LR\n  A -->|"결제 (PG)"| B',
    ),
    'ampersand-chain': (
        'flowchart LR\n  A & B --> C & D',
        True, True,
        'flowchart LR\n  A & B --> C & D',
    ),
    'link-kinds': (
        'flowchart LR\n  A --o B\n  B <--> C\n  C -.->|점선| D\n  D ~~~ E\n  E ==> F\n  F --x G',
        True, True,
        'flowchart LR\n  A --o B\n  B <--> C\n  C -.->|점선| D\n  D ~~~ E\n  E ==> F\n  F --x G',
    ),
    'long-link': (
        'flowchart LR\n  A ---> B\n  B ----> C',
        True, True,
        'flowchart LR\n  A ---> B\n  B ----> C',
    ),
    'br-in-label': (
        'flowchart LR\n  A["첫줄<br>둘째줄"] --> B',
        True, True,
        'flowchart LR\n  A["첫줄<br>둘째줄"] --> B',
    ),
    'unicode-node-id': (
        'flowchart LR\n  검증기 --> 결과',
        True, True,
        'flowchart LR\n  검증기 --> 결과',
    ),
    'hyphen-node-id': (
        'flowchart LR\n  api-gw --> svc',
        True, True,
        'flowchart LR\n  api-gw --> svc',
    ),
    'nested-quotes-escaped': (
        'flowchart LR\n  A["그는 #quot;안녕#quot;"] --> B',
        True, True,
        'flowchart LR\n  A["그는 #quot;안녕#quot;"] --> B',
    ),
    'two-quoted-labels': (
        'flowchart LR\n  A["x"] --> B["y"]',
        True, True,
        'flowchart LR\n  A["x"] --> B["y"]',
    ),
    'single-quotes-in-label': (
        "flowchart LR\n  A['작은 따옴표'] --> B",
        True, True,
        "flowchart LR\n  A['작은 따옴표'] --> B",
    ),
    'note-colon-in-text': (
        'flowchart LR\n  A --> B\n  Note right of A: 시간: 3초',
        False, True,
        'flowchart LR\n  A --> B\n  A -.- mado_note_1["시간: 3초"]\n  classDef madoNote fill:#fef9c3,stroke:#ca8a04,color:#713f12\n  class mado_note_1 madoNote',
    ),
    'note-brackets-in-text': (
        'flowchart LR\n  A --> B\n  Note right of A: 배열[0] (첫 원소)',
        False, True,
        'flowchart LR\n  A --> B\n  A -.- mado_note_1["배열[0] (첫 원소)"]\n  classDef madoNote fill:#fef9c3,stroke:#ca8a04,color:#713f12\n  class mado_note_1 madoNote',
    ),
    'note-hash-in-text': (
        'flowchart LR\n  A --> B\n  Note right of A: 우선순위 #1',
        False, True,
        'flowchart LR\n  A --> B\n  A -.- mado_note_1["우선순위 #1"]\n  classDef madoNote fill:#fef9c3,stroke:#ca8a04,color:#713f12\n  class mado_note_1 madoNote',
    ),
    'note-semicolon': (
        'flowchart LR\n  A --> B\n  Note right of A: 끝;',
        False, True,
        'flowchart LR\n  A --> B\n  A -.- mado_note_1["끝;"]\n  classDef madoNote fill:#fef9c3,stroke:#ca8a04,color:#713f12\n  class mado_note_1 madoNote',
    ),
    'note-over-spaced-comma': (
        'flowchart LR\n  A --> B\n  Note over A , B: 둘',
        False, True,
        'flowchart LR\n  A --> B\n  A -.- mado_note_1["둘"]\n  B -.- mado_note_1\n  classDef madoNote fill:#fef9c3,stroke:#ca8a04,color:#713f12\n  class mado_note_1 madoNote',
    ),
    'note-unicode-target': (
        'flowchart LR\n  검증기 --> B\n  Note right of 검증기: x',
        False, False,
        'flowchart LR\n  검증기 --> B\n  Note right of 검증기: x',
    ),
    'note-hyphen-target': (
        'flowchart LR\n  api-gw --> B\n  Note right of api-gw: x',
        False, False,
        'flowchart LR\n  api-gw --> B\n  Note right of api-gw: x',
    ),
    'note-existing-mado-node': (
        'flowchart LR\n  A --> mado_note_1["기존"]\n  Note right of A: 새 노트',
        False, True,
        'flowchart LR\n  A --> mado_note_1["기존"]\n  A -.- mado_note_2["새 노트"]\n  classDef madoNote fill:#fef9c3,stroke:#ca8a04,color:#713f12\n  class mado_note_2 madoNote',
    ),
    'note-existing-classdef': (
        'flowchart LR\n  A --> B\n  classDef madoNote fill:#fff\n  Note right of A: x',
        False, True,
        'flowchart LR\n  A --> B\n  classDef madoNote fill:#fff\n  A -.- mado_note_1["x"]\n  class mado_note_1 madoNote',
    ),
    'note-twice': (
        'flowchart LR\n  A --> B\n  Note right of A: 하나\n  Note left of B: 둘',
        False, True,
        'flowchart LR\n  A --> B\n  A -.- mado_note_1["하나"]\n  B -.- mado_note_2["둘"]\n  classDef madoNote fill:#fef9c3,stroke:#ca8a04,color:#713f12\n  class mado_note_1,mado_note_2 madoNote',
    ),
    'note-in-comment': (
        'flowchart LR\n  A --> B\n  %% Note right of A: 주석',
        True, True,
        'flowchart LR\n  A --> B\n  %% Note right of A: 주석',
    ),
    'note-uppercase': (
        'flowchart LR\n  A --> B\n  NOTE RIGHT OF A: 대문자',
        False, True,
        'flowchart LR\n  A --> B\n  A -.- mado_note_1["대문자"]\n  classDef madoNote fill:#fef9c3,stroke:#ca8a04,color:#713f12\n  class mado_note_1 madoNote',
    ),
    'note-backtick': (
        'flowchart LR\n  A --> B\n  Note right of A: `코드` 사용',
        False, True,
        'flowchart LR\n  A --> B\n  A -.- mado_note_1["\'코드\' 사용"]\n  classDef madoNote fill:#fef9c3,stroke:#ca8a04,color:#713f12\n  class mado_note_1 madoNote',
    ),
    'note-lt-gt': (
        'flowchart LR\n  A --> B\n  Note right of A: a < b > c',
        False, True,
        'flowchart LR\n  A --> B\n  A -.- mado_note_1["a < b > c"]\n  classDef madoNote fill:#fef9c3,stroke:#ca8a04,color:#713f12\n  class mado_note_1 madoNote',
    ),
    'note-graph-semicolons': (
        'graph TD;\n  A-->B;\n  Note right of A: x;',
        False, True,
        'graph TD;\n  A-->B;\n  A -.- mado_note_1["x;"]\n  classDef madoNote fill:#fef9c3,stroke:#ca8a04,color:#713f12\n  class mado_note_1 madoNote',
    ),
    'sequence-message-bracket-paren': (
        'sequenceDiagram\n  A->>B: 배열[0] (첫번째) 조회',
        True, True,
        'sequenceDiagram\n  A->>B: 배열[0] (첫번째) 조회',
    ),
    'sequence-note-bracket-paren': (
        'sequenceDiagram\n  A->>B: x\n  Note right of A: 배열[0] (첫번째)',
        True, True,
        'sequenceDiagram\n  A->>B: x\n  Note right of A: 배열[0] (첫번째)',
    ),
    'gantt-task-bracket': (
        'gantt\n  title 일정\n  section A\n  작업[1] (준비) :a1, 2024-01-01, 3d',
        True, True,
        'gantt\n  title 일정\n  section A\n  작업[1] (준비) :a1, 2024-01-01, 3d',
    ),
    'class-method-bracket': (
        'classDiagram\n  class A {\n    +get(int[] (x)) int\n  }',
        True, True,
        'classDiagram\n  class A {\n    +get(int[] (x)) int\n  }',
    ),
    'er-attr': (
        'erDiagram\n  USER {\n    string name "이름 (실명)"\n  }',
        True, True,
        'erDiagram\n  USER {\n    string name "이름 (실명)"\n  }',
    ),
    'mindmap-paren': (
        'mindmap\n  root((중심))\n    가지[항목 (A)]',
        False, True,
        'mindmap\n  root((중심))\n    가지["항목 (A)"]',
    ),
    'state-bracket-paren': (
        'stateDiagram-v2\n  s1 : 상태[1] (대기)\n  [*] --> s1',
        True, True,
        'stateDiagram-v2\n  s1 : 상태[1] (대기)\n  [*] --> s1',
    ),
    'seq-rule-activate-node': (
        'flowchart LR\n  activate_user --> B',
        True, True,
        'flowchart LR\n  activate_user --> B',
    ),
    'seq-rule-participants': (
        'flowchart LR\n  participants --> B',
        True, True,
        'flowchart LR\n  participants --> B',
    ),
    'seq-rule-and-label': (
        'flowchart LR\n  and["그리고"] --> B',
        True, True,
        'flowchart LR\n  and["그리고"] --> B',
    ),
    'seq-rule-note-edge-label': (
        'flowchart LR\n  A -->|note right of| B',
        True, True,
        'flowchart LR\n  A -->|note right of| B',
    ),
    'seq-arrow-in-label-quoted': (
        'flowchart LR\n  A -->|"a->>b"| B',
        True, True,
        'flowchart LR\n  A -->|"a->>b"| B',
    ),
    'seq-dash-x-in-label': (
        'flowchart LR\n  A -- -x -- B',
        False, False,
        'flowchart LR\n  A -- -x -- B',
    ),
    'subgraph-named-loop': (
        'graph TD\n  subgraph loop 재시도\n    A --> B\n  end',
        True, True,
        'graph TD\n  subgraph loop 재시도\n    A --> B\n  end',
    ),
    'direction-in-subgraph': (
        'graph TD\n  subgraph S\n    direction LR\n    A --> B\n  end',
        True, True,
        'graph TD\n  subgraph S\n    direction LR\n    A --> B\n  end',
    ),
    'only-comments': (
        '%% 주석만',
        False, False,
        '%% 주석만',
    ),
    'header-with-trailing-spaces': (
        'flowchart LR   \n  A --> B',
        True, True,
        'flowchart LR   \n  A --> B',
    ),
    'html-entity-lt': (
        'flowchart LR\n  A["a &lt; b"] --> B',
        True, True,
        'flowchart LR\n  A["a &lt; b"] --> B',
    ),
    'stadium-paren': (
        'flowchart LR\n  A([대기 (큐)]) --> B',
        False, True,
        'flowchart LR\n  A(["대기 (큐)"]) --> B',
    ),
    'asym-paren-2': (
        'flowchart LR\n  A>결제 (PG)] --> B[주문]',
        False, True,
        'flowchart LR\n  A>"결제 (PG)"] --> B[주문]',
    ),
    'asym-after-arrow': (
        'flowchart LR\n  X --> A>결제 (PG)]',
        False, True,
        'flowchart LR\n  X --> A>"결제 (PG)"]',
    ),
    'style-rgba': (
        'flowchart LR\n  A --> B\n  style A fill:rgba(0,0,0,0.5),stroke:rgb(255, 0, 0)',
        False, True,
        'flowchart LR\n  A --> B\n  style A fill:#00000080,stroke:#ff0000',
    ),
    'style-rgb-percent-alpha': (
        'flowchart LR\n  A --> B\n  classDef c fill:rgba(10,20,30,50%)',
        False, True,
        'flowchart LR\n  A --> B\n  classDef c fill:#0a141e80',
    ),
    'style-hsl': (
        'flowchart LR\n  A --> B\n  style A fill:hsl(0,100%,50%)',
        False, False,
        'flowchart LR\n  A --> B\n  style A fill:hsl(0,100%,50%)',
    ),
    'linkstyle-rgb': (
        'flowchart LR\n  A --> B\n  linkStyle 0 stroke:rgb(1,2,3)',
        False, True,
        'flowchart LR\n  A --> B\n  linkStyle 0 stroke:#010203',
    ),
    'style-rgb-out-of-range': (
        'flowchart LR\n  A --> B\n  style A fill:rgb(300,0,0)',
        False, False,
        'flowchart LR\n  A --> B\n  style A fill:rgb(300,0,0)',
    ),
    'subgraph-bare-paren-2': (
        'flowchart TD\n  subgraph 결제 영역 (PG)\n    A --> B\n  end\n  B --> C',
        False, True,
        'flowchart TD\n  subgraph "결제 영역 (PG)"\n    A --> B\n  end\n  B --> C',
    ),
    'subgraph-id-quoted-title': (
        'flowchart TD\n  subgraph pay "결제 (PG)"\n    A --> B\n  end',
        False, True,
        'flowchart TD\n  subgraph pay["결제 (PG)"]\n    A --> B\n  end',
    ),
    'front-matter-note': (
        '---\ntitle: x\n---\nflowchart LR\n  A --> B\n  Note right of A: 노트',
        False, True,
        '---\ntitle: x\n---\nflowchart LR\n  A --> B\n  A -.- mado_note_1["노트"]\n  classDef madoNote fill:#fef9c3,stroke:#ca8a04,color:#713f12\n  class mado_note_1 madoNote',
    ),
    'bom-note': (
        '\ufeffflowchart LR\n  A --> B\n  Note right of A: 노트',
        False, True,
        'flowchart LR\n  A --> B\n  A -.- mado_note_1["노트"]\n  classDef madoNote fill:#fef9c3,stroke:#ca8a04,color:#713f12\n  class mado_note_1 madoNote',
    ),
    'note-renormalize': (
        'flowchart LR\n  A --> B\n  A -.- mado_note_1["첫"]\n  classDef madoNote fill:#fef9c3\n  class mado_note_1 madoNote\n  Note right of B: 둘',
        False, True,
        'flowchart LR\n  A --> B\n  A -.- mado_note_1["첫"]\n  classDef madoNote fill:#fef9c3\n  class mado_note_1 madoNote\n  B -.- mado_note_2["둘"]\n  class mado_note_2 madoNote',
    ),
    'seq-msg-paren-bracket': (
        'sequenceDiagram\n  A->>B: 호출[결제 (PG)]',
        True, True,
        'sequenceDiagram\n  A->>B: 호출[결제 (PG)]',
    ),
    'state-paren-bracket': (
        'stateDiagram-v2\n  s1 : 상태[결제 (PG)]\n  [*] --> s1',
        True, True,
        'stateDiagram-v2\n  s1 : 상태[결제 (PG)]\n  [*] --> s1',
    ),
    'gantt-paren-bracket': (
        'gantt\n  title 일정\n  section A\n  작업[결제 (PG)] :a1, 2024-01-01, 3d',
        True, True,
        'gantt\n  title 일정\n  section A\n  작업[결제 (PG)] :a1, 2024-01-01, 3d',
    ),
    'class-paren-bracket': (
        'classDiagram\n  class A {\n    +pay[결제 (PG)] int\n  }',
        True, True,
        'classDiagram\n  class A {\n    +pay[결제 (PG)] int\n  }',
    ),
    'journey-paren-bracket': (
        'journey\n  title 여정\n  section 시작\n    로그인[계정 (SSO)]: 5: 사용자',
        True, True,
        'journey\n  title 여정\n  section 시작\n    로그인[계정 (SSO)]: 5: 사용자',
    ),
    'timeline-paren-bracket': (
        'timeline\n  title 역사\n  2024 : 출시[베타 (A)]',
        True, True,
        'timeline\n  title 역사\n  2024 : 출시[베타 (A)]',
    ),
    'end-semicolon-2': (
        'graph TD\n  subgraph S\n    A --> B\n  end;\n  B --> C',
        True, True,
        'graph TD\n  subgraph S\n    A --> B\n  end;\n  B --> C',
    ),
    'opt-class-colon-space': (
        'flowchart LR\n  opt : x',
        False, False,
        'flowchart LR\n  opt : x',
    ),
    'subroutine-paren': (
        'flowchart LR\n  A[[서브 (x)]] --> B',
        False, True,
        'flowchart LR\n  A[["서브 (x)"]] --> B',
    ),
    'parallelogram-paren': (
        'flowchart LR\n  A[/입력 (x)/] --> B',
        False, True,
        'flowchart LR\n  A[/"입력 (x)"/] --> B',
    ),
    'hexagon-paren': (
        'flowchart LR\n  A{{육각 (x)}} --> B',
        False, False,
        'flowchart LR\n  A{{육각 (x)}} --> B',
    ),
    'double-circle-paren': (
        'flowchart LR\n  A((원 (x))) --> B',
        False, True,
        'flowchart LR\n  A(("원 (x)")) --> B',
    ),
    'none-input': (
        '',
        False, False,
        '',
    ),
    'trapezoid-paren': (
        'flowchart LR\n  A[/입력 (x)\\] --> B[\\출력 (y)/]',
        False, True,
        'flowchart LR\n  A[/"입력 (x)"\\] --> B[\\"출력 (y)"/]',
    ),
    'reverse-para-paren': (
        'flowchart LR\n  A[\\입력 (x)\\] --> B',
        False, True,
        'flowchart LR\n  A[\\"입력 (x)"\\] --> B',
    ),
    'subgraph-id-quoted-noparen': (
        'flowchart TD\n  subgraph pay "결제"\n    A --> B\n  end',
        False, True,
        'flowchart TD\n  subgraph pay["결제"]\n    A --> B\n  end',
    ),
    'subgraph-id-bracket-ok': (
        'flowchart TD\n  subgraph pay["결제 (PG)"]\n    A --> B\n  end',
        True, True,
        'flowchart TD\n  subgraph pay["결제 (PG)"]\n    A --> B\n  end',
    ),
    'double-circle-quoted-ok': (
        'flowchart LR\n  A(("원 (x)")) --> B',
        True, True,
        'flowchart LR\n  A(("원 (x)")) --> B',
    ),
    'arrow-then-double-paren': (
        'flowchart LR\n  A --> B((원))',
        True, True,
        'flowchart LR\n  A --> B((원))',
    ),
    'round-inside-label-text': (
        'flowchart LR\n  A["f((x))"] --> B',
        True, True,
        'flowchart LR\n  A["f((x))"] --> B',
    ),
    'slash-in-edge-label': (
        'flowchart LR\n  A -->|a/b (c)| B',
        False, False,
        'flowchart LR\n  A -->|a/b (c)| B',
    ),
    'url-in-click': (
        'flowchart LR\n  A --> B\n  click A "https://x.io/a/(b)/c"',
        True, True,
        'flowchart LR\n  A --> B\n  click A "https://x.io/a/(b)/c"',
    ),
}


def _clean(text):
    return text.replace("\ufeff", "").replace("\r\n", "\n").strip()


@pytest.mark.parametrize("name", sorted(n for n, v in ORACLE.items() if v[2]))
def test_no_false_positive_on_what_the_parser_accepts(name):
    raw, _raw_ok, _norm_ok, _norm = ORACLE[name]
    issues = lint_mermaid(normalize_mermaid(raw))
    assert issues == [], f"{name}: 파서가 받는 다이어그램을 지적했습니다 -> {format_issues(issues)}"


@pytest.mark.parametrize("name", sorted(n for n, v in ORACLE.items() if not v[2] and _clean(v[0])))
def test_what_the_parser_rejects_is_caught(name):
    raw, _raw_ok, _norm_ok, _norm = ORACLE[name]
    assert lint_mermaid(normalize_mermaid(raw)), f"{name}: 렌더러가 거부할 다이어그램을 통과시켰습니다"


@pytest.mark.parametrize("name", sorted(ORACLE))
def test_normalized_text_is_the_one_the_parser_judged(name):
    raw, _raw_ok, _norm_ok, norm = ORACLE[name]
    assert normalize_mermaid(raw) == norm


@pytest.mark.parametrize("name", sorted(ORACLE))
def test_normalize_is_idempotent(name):
    once = normalize_mermaid(ORACLE[name][0])
    assert normalize_mermaid(once) == once


@pytest.mark.parametrize("name", sorted(
    n for n, v in ORACLE.items() if v[1] and diagram_kind(v[0]) not in ("graph", "flowchart", "mindmap")
))
def test_valid_non_flowchart_text_is_left_exactly_as_written(name):
    """시퀀스·상태·간트 등에서는 `[결제 (PG)]` 가 정상입니다. 따옴표를 씌우면 보이는 글자가 바뀝니다."""
    raw = ORACLE[name][0]
    assert normalize_mermaid(raw) == _clean(raw)


def test_a_rejected_diagram_is_never_made_worse():
    """원문을 파서가 받았다면 정규화본도 받아야 합니다 (기준표 전체에 대한 확인)."""
    broken = [n for n, v in ORACLE.items() if v[1] and not v[2]]
    assert broken == []


@pytest.mark.parametrize("value", [None, "", "   ", "\ufeff", "\r\n\r\n"])
def test_empty_or_missing_input_does_not_crash(value):
    assert normalize_mermaid(value) == ""
    assert [i.rule for i in lint_mermaid(value)] == ["empty"]


# ------------------------------------------------------------------ 블록 추출


from app.orchestration.engine import OrchestratorEngine, extract_code_blocks, find_mermaid_blocks  # noqa: E402

FENCE = "`" * 3


def _langs(text):
    return [(b["language"], b["code"].splitlines()[0]) for b in extract_code_blocks(text)]


def test_an_info_string_does_not_shift_every_later_fence():
    """회귀: `mermaid title="…"` 을 여는 펜스로 못 읽어, 뒤따르는 python 블록까지 사라졌습니다."""
    text = f'설명\n{FENCE}mermaid title="흐름"\nflowchart LR\n  A --> B\n{FENCE}\n\n{FENCE}python\nprint(1)\n{FENCE}'
    assert _langs(text) == [("mermaid", "flowchart LR"), ("python", "print(1)")]


def test_tilde_fences_are_code_blocks():
    assert _langs("~~~mermaid\nflowchart LR\n  A --> B\n~~~") == [("mermaid", "flowchart LR")]


@pytest.mark.parametrize("first", [
    "%%{init: {'theme':'dark'}}%%",
    "---\ntitle: x\n---",
    "%% 주석",
])
def test_untagged_blocks_with_a_preamble_are_mermaid(first):
    text = f"{FENCE}\n{first}\nflowchart LR\n  A --> B\n{FENCE}"
    assert [b["language"] for b in extract_code_blocks(text)] == ["mermaid"]
    assert len(find_mermaid_blocks(text)) == 1


@pytest.mark.parametrize("header", ["kanban", "packet-beta", "zenuml", "C4Container"])
def test_untagged_newer_diagram_kinds_are_mermaid(header):
    assert [b["language"] for b in extract_code_blocks(f"{FENCE}\n{header}\n  x\n{FENCE}")] == ["mermaid"]


def test_untagged_prose_is_not_mermaid():
    """`pie` 로 시작하는 글이 파이 차트가 되면 안 됩니다 (예전 접두사 비교는 `pieces` 도 받았습니다)."""
    assert [b["language"] for b in extract_code_blocks(f"{FENCE}\npieces of text\n{FENCE}")] == ["text"]


def test_inline_triple_backticks_in_prose_do_not_open_a_block():
    text = f"설명에 {FENCE} 가 나오고\n{FENCE}mermaid\ngraph TD\n A-->B\n{FENCE}"
    assert _langs(text) == [("mermaid", "graph TD")]


def test_a_longer_outer_fence_keeps_its_inner_example_as_text():
    text = f"````markdown\n{FENCE}mermaid\ngraph TD\n A-->B\n{FENCE}\n````"
    assert [b["language"] for b in extract_code_blocks(text)] == ["markdown"]
    assert find_mermaid_blocks(text) == [], "문서 속 예시는 산출물 다이어그램이 아닙니다"


@pytest.mark.parametrize("text", [
    f"{FENCE}mermaid\ngraph TD\n A-->B",                       # 응답 한도로 잘림
    f"{FENCE}mermaid\ngraph TD\n A-->B\n{FENCE} 끝",            # 닫는 줄에 글
    f"{FENCE}mermaid\ngraph TD\n A-->B{FENCE}",                 # 코드 줄 끝에 펜스
    f"  {FENCE}mermaid\n  graph TD\n   A-->B\n  {FENCE}",       # 목록 안 들여쓰기
    f"다음과 같습니다: {FENCE}mermaid\ngraph TD\n A-->B\n{FENCE}",  # 줄 중간에서 연 펜스
    f"{FENCE}Mermaid   \r\ngraph TD\r\n A-->B\r\n{FENCE}",       # 대문자·뒤 공백·CRLF
])
def test_sloppy_but_common_fences_still_yield_the_diagram(text):
    blocks = find_mermaid_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["code"].replace("\r", "").splitlines()[0].strip() == "graph TD"
    assert FENCE not in blocks[0]["code"]


def test_empty_and_adjacent_blocks():
    text = f"{FENCE}mermaid\n{FENCE}\n{FENCE}mermaid\ngraph TD\n A-->B\n{FENCE}\n{FENCE}mermaid\ngraph TD\n C-->D\n{FENCE}"
    assert [b["code"].splitlines()[1].strip() for b in find_mermaid_blocks(text)] == ["A-->B", "C-->D"]


@pytest.mark.parametrize("text", [
    f'앞\n{FENCE}mermaid title="x"\ngraph TD\n A-->B\n{FENCE}\n뒤',
    "앞\n~~~mermaid\ngraph TD\n A-->B\n~~~\n뒤",
    f"앞\r\n{FENCE}mermaid\r\ngraph TD\r\n A-->B\r\n{FENCE}\r\n뒤",
])
def test_splicing_a_fixed_diagram_keeps_the_fences_intact(text):
    """수선한 다이어그램을 제자리에 끼운 뒤에도 펜스가 깨지지 않고, 앞뒤 글이 그대로여야 합니다."""
    blocks = find_mermaid_blocks(text)
    out = OrchestratorEngine._splice_blocks(text, [(0, blocks[0], [])], ["graph LR\n X-->Y"])
    assert out.startswith("앞") and out.rstrip().endswith("뒤")
    again = find_mermaid_blocks(out)
    assert [b["code"] for b in again] == ["graph LR\n X-->Y"]

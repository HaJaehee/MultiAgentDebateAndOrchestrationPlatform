import json
import pytest

from app.export_mermaid import (
    MermaidToStarUMLConverter,
    convert_mermaid_to_staruml_mdj,
    generate_mermaid_standalone_html,
)
from app.ui.components.artifact_viewer import _clean_title_for_filename


def test_clean_title_for_filename():
    assert _clean_title_for_filename("시스템 아키텍처 다이어그램 #1") == "시스템_아키텍처_다이어그램_1"
    assert _clean_title_for_filename("My Class Diagram (v2.0)") == "My_Class_Diagram_v20"
    assert _clean_title_for_filename("", default="fallback") == "fallback"


def test_flowchart_to_staruml():
    mermaid_code = """graph TD
    A[웹 클라이언트 (User Web)] -->|HTTP API| B[API 게이트웨이]
    B --> C[인증 서비스]
    B --> D[주문 서비스]
    D --> E[(주문 PostgreSQL DB)]
    C --> F[(Redis 세션 저장소)]
    """
    mdj_str = convert_mermaid_to_staruml_mdj("E-Commerce Architecture", mermaid_code)
    assert mdj_str is not None
    data = json.loads(mdj_str)

    assert data["_type"] == "Project"
    assert data["name"] == "E-Commerce Architecture"
    assert len(data["ownedElements"]) == 1

    model = data["ownedElements"][0]
    assert model["_type"] == "UMLModel"
    assert model["name"] == "ArchitectureModel"

    # Owned elements should include 1 diagram + components
    diagram = model["ownedElements"][0]
    assert diagram["_type"] == "UMLComponentDiagram"
    assert diagram["name"] == "E-Commerce Architecture"
    assert diagram["visible"] is True
    assert len(diagram["ownedViews"]) > 0

    # Check that component views and dependency views exist
    comp_views = [v for v in diagram["ownedViews"] if v["_type"] == "UMLComponentView"]
    assert len(comp_views) >= 5

    dep_views = [v for v in diagram["ownedViews"] if v["_type"] == "UMLDependencyView"]
    assert len(dep_views) >= 4

    # Check documentation contains raw mermaid code
    assert "Original Mermaid Code:" in data["documentation"]
    assert "웹 클라이언트" in data["documentation"]


def test_class_diagram_to_staruml():
    mermaid_code = """classDiagram
    class UserService {
        +String userId
        +String email
        +login(String email, String password) Boolean
        +getUserProfile() User
    }
    class AuthService {
        +generateToken(User user) String
        +validateToken(String token) Boolean
    }
    class User {
        +String id
        +String name
    }
    UserService --> AuthService : uses
    UserService --> User : manages
    """
    mdj_str = convert_mermaid_to_staruml_mdj("User Service Class Model", mermaid_code)
    data = json.loads(mdj_str)

    assert data["_type"] == "Project"
    model = data["ownedElements"][0]
    assert model["name"] == "ClassModel"

    diagram = model["ownedElements"][0]
    assert diagram["_type"] == "UMLClassDiagram"

    classes = [elem for elem in model["ownedElements"][1:] if elem["_type"] == "UMLClass"]
    assert len(classes) == 3

    user_service = next(c for c in classes if c["name"] == "UserService")
    attr_names = [a["name"] for a in user_service["attributes"]]
    assert any("userId" in a for a in attr_names)
    assert any(a.get("type") == "String" for a in user_service["attributes"])

    # Check operation names do NOT contain parentheses
    for op in user_service["operations"]:
        assert "(" not in op["name"]
        assert ")" not in op["name"]

    login_op = next(o for o in user_service["operations"] if o["name"] == "login")
    assert login_op["visibility"] == "public"
    param_names = [p["name"] for p in login_op["parameters"]]
    assert "email" in param_names
    assert "password" in param_names
    assert "return" in param_names
    return_param = next(p for p in login_op["parameters"] if p["name"] == "return")
    assert return_param["type"] == "Boolean"
    assert return_param["direction"] == "return"

    # Check relationships exist in model and diagram views
    assoc_views = [v for v in diagram["ownedViews"] if v["_type"] == "UMLAssociationView"]
    assert len(assoc_views) == 2
    assert any(v.get("nameLabel", {}).get("text") == "uses" for v in assoc_views)
    assert any(v.get("nameLabel", {}).get("text") == "manages" for v in assoc_views)


def test_sequence_diagram_to_staruml():
    mermaid_code = """sequenceDiagram
    autonumber
    actor User as 사용자
    participant Gateway as API 게이트웨이
    participant Auth as 인증 서버
    participant DB as 데이터베이스

    User->>Gateway: 로그인 요청
    Gateway->>Auth: 토큰 검증 요청
    Auth->>DB: 사용자 정보 조회
    DB-->>Auth: 조회 결과 반환
    Auth-->>Gateway: 인증 토큰 발급
    Gateway-->>User: 로그인 응답 (200 OK)
    """
    mdj_str = convert_mermaid_to_staruml_mdj("Login Sequence Flow", mermaid_code)
    data = json.loads(mdj_str)

    assert data["_type"] == "Project"
    model = data["ownedElements"][0]
    assert model["name"] == "SequenceModel"

    interaction = model["ownedElements"][0]
    assert interaction["_type"] == "UMLInteraction"
    assert len(interaction["participants"]) >= 4
    assert len(interaction["messages"]) >= 5

    diagram = interaction["ownedElements"][0]
    assert diagram["_type"] == "UMLSequenceDiagram"

    lifeline_views = [v for v in diagram["ownedViews"] if v["_type"] == "UMLSeqLifelineView"]
    assert len(lifeline_views) == 4
    for lv in lifeline_views:
        assert "linePart" in lv and "$ref" in lv["linePart"]
        assert "nameCompartment" in lv and "$ref" in lv["nameCompartment"]
        assert "subViews" in lv
        sub_types = [sv["_type"] for sv in lv["subViews"]]
        assert "UMLNameCompartmentView" in sub_types
        assert "UMLLinePartView" in sub_types

    msg_views = [v for v in diagram["ownedViews"] if v["_type"] == "UMLSeqMessageView"]
    assert len(msg_views) == 6
    for mv in msg_views:
        assert "head" in mv and "$ref" in mv["head"]
        assert "tail" in mv and "$ref" in mv["tail"]
        assert "nameLabel" in mv and "$ref" in mv["nameLabel"]
        assert "stereotypeLabel" in mv and "$ref" in mv["stereotypeLabel"]
        assert "propertyLabel" in mv and "$ref" in mv["propertyLabel"]
        assert "activation" in mv and "$ref" in mv["activation"]
        assert "subViews" in mv
        sub_types = [sv["_type"] for sv in mv["subViews"]]
        assert "EdgeLabelView" in sub_types
        assert "UMLActivationView" in sub_types
        assert ":" in mv["points"]


def test_complex_sequence_diagram_7_lifelines_11_messages():
    mermaid_code = """sequenceDiagram
    autonumber
    actor User as 사용자
    participant Gateway as API 게이트웨이
    participant Auth as 인증 서버
    participant UserSvc as 사용자 서비스
    participant OrderSvc as 주문 서비스
    participant PaymentSvc as 결제 서비스
    participant DB as 데이터베이스

    User->>Gateway: 1. 주문 생성 요청
    Gateway->>Auth: 2. 토큰 검증 요청
    Auth-->>Gateway: 3. 토큰 유효 응답
    Gateway->>UserSvc: 4. 사용자 등급 조회
    UserSvc-->>Gateway: 5. 등급 정보 반환
    Gateway->>OrderSvc: 6. 주문 처리 요청
    OrderSvc->>DB: 7. 재고 확인 및 차감
    DB-->>OrderSvc: 8. 처리 완료
    OrderSvc->>PaymentSvc: 9. 결제 승인 요청
    PaymentSvc-->>OrderSvc: 10. 결제 성공 응답
    OrderSvc-->>User: 11. 최종 주문 완료
    """
    mdj_str = convert_mermaid_to_staruml_mdj("E-Commerce Order Sequence", mermaid_code)
    data = json.loads(mdj_str)

    interaction = data["ownedElements"][0]["ownedElements"][0]
    assert len(interaction["participants"]) == 7
    assert len(interaction["messages"]) == 11

    diagram = interaction["ownedElements"][0]
    lifeline_views = [v for v in diagram["ownedViews"] if v["_type"] == "UMLSeqLifelineView"]
    msg_views = [v for v in diagram["ownedViews"] if v["_type"] == "UMLSeqMessageView"]

    assert len(lifeline_views) == 7
    assert len(msg_views) == 11

    # Verify Y coordinates are strictly ascending
    y_coords = []
    for mv in msg_views:
        # points format: x1:y1;x2:y2
        pts = [p.split(":") for p in mv["points"].split(";")]
        y0 = int(pts[0][1])
        y_coords.append(y0)

    assert y_coords == sorted(y_coords)
    assert len(set(y_coords)) == 11  # All distinct vertical levels

    # Verify activations
    sync_msgs = [mv for mv in msg_views if any(sv["_type"] == "UMLActivationView" and sv.get("visible") is True for sv in mv["subViews"])]
    reply_msgs = [mv for mv in msg_views if any(sv["_type"] == "UMLActivationView" and sv.get("visible") is False for sv in mv["subViews"])]
    assert len(sync_msgs) == 6
    assert len(reply_msgs) == 5



def test_empty_mermaid_to_staruml():
    mdj_str = convert_mermaid_to_staruml_mdj("Empty Diagram", "")
    data = json.loads(mdj_str)
    assert data["_type"] == "Project"
    assert data["name"] == "Empty Diagram"


def test_generate_mermaid_standalone_html():
    mermaid_code = "graph TD\n    A[Start] --> B[End]"
    html_content = generate_mermaid_standalone_html("Sample Process", mermaid_code)

    assert "<!DOCTYPE html>" in html_content
    assert "Sample Process" in html_content
    assert "cdn.jsdelivr.net/npm/mermaid" in html_content
    assert "btn-download-png" in html_content
    assert "btn-download-svg" in html_content
    assert "btn-copy-img" in html_content
    assert "btn-toggle-view" in html_content
    assert "btn-theme" in html_content
    assert "A[Start] --&gt; B[End]" in html_content or "A[Start] --> B[End]" in html_content


def test_generate_mermaid_standalone_html_with_svg():
    mermaid_code = "graph TD\n    A --> B"
    svg_mock = '<svg width="200" height="100"><circle cx="50" cy="50" r="40" /></svg>'
    html_content = generate_mermaid_standalone_html("Custom SVG Test", mermaid_code, svg_content=svg_mock)

    assert "<circle cx=\"50\" cy=\"50\" r=\"40\"" in html_content
    assert "Custom SVG Test" in html_content


# ------------------------------------------------- 독립 실행형 HTML 이 정말 독립적인가
#
# 증상: 내려받은 다이어그램 HTML 을 열면 흰 백지만 나왔습니다.
#
# 원인은 두 가지가 겹친 것이었습니다.
#   1. 문서가 열릴 때마다 CDN 에서 Mermaid 렌더러를 받아 와야 했습니다. 폐쇄망·
#      비행기·사내망에서는 못 받습니다. 이 프로젝트는 폐쇄망 배포를 전제로
#      하는데도 "독립 실행형" 문서가 인터넷을 요구했습니다.
#   2. 못 받으면 스크립트 첫 줄 `mermaid.initialize(...)` 가 ReferenceError 로
#      죽어 툴바·줌·저장까지 전부 멈췄고, 그 자리에 남은 원본 소스는 다크 테마의
#      흰 글자라 흰 판 위에서 보이지 않았습니다 — 그래서 "흰 백지".


def test_embedded_svg_makes_the_document_offline_capable():
    """렌더링된 SVG 를 심으면 CDN 을 아예 부르지 않습니다."""
    html_content = generate_mermaid_standalone_html(
        "Architecture",
        "graph TD\n    A --> B",
        svg_content='<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>',
    )

    assert "cdn.jsdelivr.net" not in html_content
    assert "<script src=" not in html_content
    assert "const HAS_EMBEDDED_SVG = true" in html_content
    assert "<rect width=\"10\" height=\"10\"/>" in html_content


def test_missing_renderer_does_not_kill_the_rest_of_the_script():
    """SVG 가 없어 CDN 에 기대야 할 때도, 렌더러가 없으면 안내를 띄우고 나머지는 삽니다."""
    html_content = generate_mermaid_standalone_html("Architecture", "graph TD\n    A --> B")

    assert "cdn.jsdelivr.net" in html_content
    assert "const HAS_EMBEDDED_SVG = false" in html_content
    # 렌더러 유무를 확인한 뒤에 초기화합니다 (예전에는 무조건 불렀습니다).
    assert "typeof mermaid === 'undefined'" in html_content
    assert "showRenderWarning" in html_content
    assert "render-warning" in html_content
    # 초기화 실패가 툴바를 끌고 가지 않도록 try 로 감쌉니다.
    init_at = html_content.index("mermaid.initialize(")
    assert "try {" in html_content[init_at - 200:init_at]


def test_fallback_source_is_readable_on_the_light_panel():
    """렌더링이 안 됐을 때 남는 원본이 흰 글자라 안 보이던 것을 막습니다."""
    html_content = generate_mermaid_standalone_html("Architecture", "graph TD\n    A --> B")

    panel = html_content[html_content.index("#diagram-content {"):]
    panel = panel[:panel.index("}")]
    assert "color: #0f172a" in panel


def test_angle_brackets_in_the_source_survive_html_parsing():
    """`<` 가 든 다이어그램이 HTML 파서에 먹혀 통째로 사라지지 않아야 합니다.

    Mermaid 는 요소의 textContent 를 읽으므로 이스케이프해도 문법은 그대로입니다.
    """
    mermaid_code = 'graph TD\n    A["List<Event>"] --> B["a & b"]'
    html_content = generate_mermaid_standalone_html("Generics", mermaid_code)

    assert "List&lt;Event&gt;" in html_content
    assert '<div class="mermaid">graph TD' in html_content
    # 살아 있는 태그로 새어 들어가면 안 됩니다.
    assert "<Event>" not in html_content


def test_download_filename_is_a_valid_js_string():
    """따옴표가 든 제목이 JS 리터럴을 깨거나 파일명에 실체 참조로 남지 않아야 합니다."""
    html_content = generate_mermaid_standalone_html("Bob's \"Big\" Diagram", "graph TD\n    A --> B")

    assert '"Bobs_Big_Diagram" + \'.png\'' in html_content
    assert '"Bobs_Big_Diagram" + \'.svg\'' in html_content
    assert "&#x27;.png" not in html_content

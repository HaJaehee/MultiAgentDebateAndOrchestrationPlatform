"""Mermaid 다이어그램을 StarUML(.mdj) 및 독립 실행형 HTML 로 변환하는 모듈.

Mermaid 산출물은 웹 브라우저뿐 아니라 아키텍처 모델링 도구(StarUML)와
독립적인 보고서(HTML)로도 자주 활용됩니다. 이 모듈은 다음을 제공합니다:

1. `convert_mermaid_to_staruml_mdj`: Mermaid(Flowchart, Class, Sequence 등)를
   StarUML 5.x/6.x 에서 바로 열 수 있는 `.mdj` JSON 프로젝트 파일로 변환합니다.
2. `generate_mermaid_standalone_html`: 줌/패닝, 테마 전환, 이미지 저장 기능이
   포함된 단일 독립형 HTML 문서를 생성합니다.
"""

import base64
import html
import json
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional


def _gen_staruml_id() -> str:
    """StarUML 호환 24자리 Base64 스타일 고유 식별자를 생성합니다."""
    uid = uuid.uuid4().bytes
    encoded = base64.b64encode(uid).decode("ascii").replace("/", "+").replace("=", "")
    return "AAAAAAFF+" + encoded[:15]


class MermaidToStarUMLConverter:
    """Mermaid 다이어그램 스크립트를 StarUML .mdj 프로젝트로 변환합니다."""

    def __init__(self, title: str = "Architecture Diagram"):
        self.title = title or "Architecture Diagram"
        self.project_id = _gen_staruml_id()
        self.model_id = _gen_staruml_id()
        self.diagram_id = _gen_staruml_id()

    def convert(self, mermaid_code: str) -> str:
        """Mermaid 코드를 파싱하여 StarUML .mdj JSON 문자열로 반환합니다."""
        raw_code = (mermaid_code or "").strip()
        lines = [
            line.strip()
            for line in raw_code.splitlines()
            if line.strip() and not line.strip().startswith("%%")
        ]

        if not lines:
            return self._build_empty_project(raw_code)

        first_line = lines[0].lower()
        if first_line.startswith(("classdiagram",)):
            return self._convert_class_diagram(lines, raw_code)
        elif first_line.startswith(("sequencediagram",)):
            return self._convert_sequence_diagram(lines, raw_code)
        else:
            # Flowchart, Graph, State, ER, Architecture 등 기본 아키텍처 다이어그램 처리
            return self._convert_flowchart_diagram(lines, raw_code)

    def _build_empty_project(self, raw_code: str) -> str:
        diagram = {
            "_type": "UMLComponentDiagram",
            "_id": self.diagram_id,
            "_parent": {"$ref": self.model_id},
            "name": self.title,
            "visible": True,
            "defaultDiagram": True,
            "ownedViews": [],
        }
        project = {
            "_type": "Project",
            "_id": self.project_id,
            "name": self.title,
            "documentation": f"Generated from MADO (Multi-Agent Debate & Orchestration)\n\n```mermaid\n{raw_code}\n```",
            "ownedElements": [
                {
                    "_type": "UMLModel",
                    "_id": self.model_id,
                    "_parent": {"$ref": self.project_id},
                    "name": "Model",
                    "ownedElements": [diagram],
                }
            ],
        }
        return json.dumps(project, indent=2, ensure_ascii=False)

    def _convert_flowchart_diagram(self, lines: List[str], raw_code: str) -> str:
        nodes: Dict[str, Dict[str, Any]] = {}
        edges: List[Dict[str, Any]] = []

        # 노드 및 연결선 정규식
        # 예: A[웹 클라이언트] -->|API 요청| B(인증 서비스)
        edge_pattern = re.compile(
            r'([A-Za-z0-9_\-]+)\s*(?:\[\"?([^\"]+?)\"?\]|\(\"?([^\"]+?)\"?\)|\{\"?([^\"]+?)\"?\}|\(\(\"?([^\"]+?)\"?\)\)|\[\[\"?([^\"]+?)\"?\]\]|\[\(\"?([^\"]+?)\"?\)\]|\[\/\"?([^\"]+?)\"?\/\])?\s*(-{1,3}>|={1,3}>|-\.->|-{1,3}|-\.-)\s*(?:\|([^\|]+)\|)?\s*([A-Za-z0-9_\-]+)\s*(?:\[\"?([^\"]+?)\"?\]|\(\"?([^\"]+?)\"?\)|\{\"?([^\"]+?)\"?\}|\(\(\"?([^\"]+?)\"?\)\)|\[\[\"?([^\"]+?)\"?\]\]|\[\(\"?([^\"]+?)\"?\)\]|\[\/\"?([^\"]+?)\"?\/\])?'
        )
        node_pattern = re.compile(
            r'([A-Za-z0-9_\-]+)\s*(?:\[\"?([^\"]+?)\"?\]|\(\"?([^\"]+?)\"?\)|\{\"?([^\"]+?)\"?\}|\(\(\"?([^\"]+?)\"?\)\)|\[\[\"?([^\"]+?)\"?\]\]|\[\(\"?([^\"]+?)\"?\)\]|\[\/\"?([^\"]+?)\"?\/\])'
        )

        for line in lines[1:]:
            line_str = line.strip()
            if not line_str or line_str.startswith(("subgraph", "end", "style", "classDef", "click", "linkStyle")):
                continue

            # 연결선 확인
            m_edge = edge_pattern.search(line_str)
            if m_edge:
                groups = m_edge.groups()
                u_id = groups[0]
                u_labels = [g for g in groups[1:8] if g is not None]
                u_label = u_labels[0].strip("\"'") if u_labels else u_id
                arrow = groups[8]
                edge_label = (groups[9] or "").strip()
                v_id = groups[10]
                v_labels = [g for g in groups[11:18] if g is not None]
                v_label = v_labels[0].strip("\"'") if v_labels else v_id

                if u_id not in nodes:
                    nodes[u_id] = {"id": u_id, "label": u_label, "elem_id": _gen_staruml_id(), "view_id": _gen_staruml_id()}
                elif u_label != u_id:
                    nodes[u_id]["label"] = u_label

                if v_id not in nodes:
                    nodes[v_id] = {"id": v_id, "label": v_label, "elem_id": _gen_staruml_id(), "view_id": _gen_staruml_id()}
                elif v_label != v_id:
                    nodes[v_id]["label"] = v_label

                edges.append({
                    "source": u_id,
                    "target": v_id,
                    "label": edge_label,
                    "elem_id": _gen_staruml_id(),
                    "view_id": _gen_staruml_id(),
                    "arrow": arrow,
                })
                continue

            # 단독 노드 확인
            m_node = node_pattern.search(line_str)
            if m_node:
                n_id = m_node.group(1)
                n_labels = [g for g in m_node.groups()[1:] if g is not None]
                n_label = n_labels[0].strip("\"'") if n_labels else n_id
                if n_id not in nodes:
                    nodes[n_id] = {"id": n_id, "label": n_label, "elem_id": _gen_staruml_id(), "view_id": _gen_staruml_id()}
                else:
                    nodes[n_id]["label"] = n_label

        if not nodes:
            nodes["Node1"] = {"id": "Node1", "label": self.title, "elem_id": _gen_staruml_id(), "view_id": _gen_staruml_id()}

        # 2D 그리드 레이아웃 좌표 계산
        node_list = list(nodes.values())
        cols = max(1, int(len(node_list) ** 0.5 + 0.5))
        node_w, node_h = 160, 70
        spacing_x, spacing_y = 80, 70
        start_x, start_y = 80, 80

        for idx, node in enumerate(node_list):
            row = idx // cols
            col = idx % cols
            node["x"] = start_x + col * (node_w + spacing_x)
            node["y"] = start_y + row * (node_h + spacing_y)
            node["width"] = node_w
            node["height"] = node_h

        owned_elements = []
        owned_views = []

        # 모델 요소 (UMLComponent)
        for node in node_list:
            comp_elem = {
                "_type": "UMLComponent",
                "_id": node["elem_id"],
                "_parent": {"$ref": self.model_id},
                "name": node["label"],
                "documentation": f"Mermaid ID: {node['id']}",
                "ownedElements": [],
            }

            # 이 노드에서 출발하는 의존성 관계
            for edge in [e for e in edges if e["source"] == node["id"]]:
                target_node = nodes.get(edge["target"])
                if target_node:
                    dep_elem = {
                        "_type": "UMLDependency",
                        "_id": edge["elem_id"],
                        "_parent": {"$ref": node["elem_id"]},
                        "name": edge["label"],
                        "source": {"$ref": node["elem_id"]},
                        "target": {"$ref": target_node["elem_id"]},
                    }
                    comp_elem["ownedElements"].append(dep_elem)

            owned_elements.append(comp_elem)

            # 다이어그램 뷰 (UMLComponentView)
            comp_view = {
                "_type": "UMLComponentView",
                "_id": node["view_id"],
                "_parent": {"$ref": self.diagram_id},
                "model": {"$ref": node["elem_id"]},
                "left": node["x"],
                "top": node["y"],
                "width": node["width"],
                "height": node["height"],
                "nameLabel": {
                    "_type": "LabelView",
                    "_id": _gen_staruml_id(),
                    "_parent": {"$ref": node["view_id"]},
                    "left": node["x"] + 10,
                    "top": node["y"] + 20,
                    "width": node["width"] - 20,
                    "height": 20,
                    "text": node["label"],
                },
            }
            owned_views.append(comp_view)

        # 연결선 뷰 (UMLDependencyView)
        for edge in edges:
            src = nodes.get(edge["source"])
            tgt = nodes.get(edge["target"])
            if src and tgt:
                dep_view = {
                    "_type": "UMLDependencyView",
                    "_id": edge["view_id"],
                    "_parent": {"$ref": self.diagram_id},
                    "model": {"$ref": edge["elem_id"]},
                    "head": {"$ref": tgt["view_id"]},
                    "tail": {"$ref": src["view_id"]},
                    "lineStyle": 1,
                    "points": f"{src['x'] + src['width']//2}:{src['y'] + src['height']//2};{tgt['x'] + tgt['width']//2}:{tgt['y'] + tgt['height']//2}",
                    "nameLabel": {
                        "_type": "EdgeLabelView",
                        "_id": _gen_staruml_id(),
                        "_parent": {"$ref": edge["view_id"]},
                        "model": {"$ref": edge["elem_id"]},
                        "visible": bool(edge["label"]),
                        "text": edge["label"],
                    },
                }
                owned_views.append(dep_view)

        diagram = {
            "_type": "UMLComponentDiagram",
            "_id": self.diagram_id,
            "_parent": {"$ref": self.model_id},
            "name": self.title,
            "visible": True,
            "defaultDiagram": True,
            "ownedViews": owned_views,
        }

        project = {
            "_type": "Project",
            "_id": self.project_id,
            "name": self.title,
            "documentation": f"Generated from MADO (Multi-Agent Debate & Orchestration)\n\nOriginal Mermaid Code:\n```mermaid\n{raw_code}\n```",
            "ownedElements": [
                {
                    "_type": "UMLModel",
                    "_id": self.model_id,
                    "_parent": {"$ref": self.project_id},
                    "name": "ArchitectureModel",
                    "ownedElements": [diagram] + owned_elements,
                }
            ],
        }
        return json.dumps(project, indent=2, ensure_ascii=False)

    def _parse_attribute(self, raw_attr: str, parent_id: str) -> Dict[str, Any]:
        """Mermaid 속성 라인을 파싱하여 StarUML UMLAttribute 객체를 생성합니다."""
        vis_map = {"+": "public", "-": "private", "#": "protected", "~": "package"}
        raw = raw_attr.strip()
        vis = "public"
        if raw and raw[0] in vis_map:
            vis = vis_map[raw[0]]
            raw = raw[1:].strip()

        # ~Generic~ 표기를 <Generic> 으로 정규화
        raw = raw.replace("~", "<", 1).replace("~", ">")

        if ":" in raw:
            aname, atype = [x.strip() for x in raw.split(":", 1)]
        else:
            parts = raw.split()
            if len(parts) >= 2:
                atype, aname = parts[0], parts[1]
            else:
                aname, atype = parts[0], ""

        attr_id = _gen_staruml_id()
        elem: Dict[str, Any] = {
            "_type": "UMLAttribute",
            "_id": attr_id,
            "_parent": {"$ref": parent_id},
            "name": aname,
            "visibility": vis,
        }
        if atype:
            elem["type"] = atype
        return elem

    def _parse_operation(self, raw_op: str, parent_id: str) -> Dict[str, Any]:
        """Mermaid 메서드 라인을 파싱하여 StarUML UMLOperation 및 UMLParameter 객체를 생성합니다.
        
        StarUML 은 메서드 렌더 시 이름 뒤에 () 를 자동 부착하므로, name 에 () 가
        들어가면 approve()() 처럼 괄호가 두 번 출력됩니다. 괄호와 인자를 온전히
        UMLParameter 구조로 분리하여 내보냅니다.
        """
        vis_map = {"+": "public", "-": "private", "#": "protected", "~": "package"}
        raw = raw_op.strip()
        vis = "public"
        if raw and raw[0] in vis_map:
            vis = vis_map[raw[0]]
            raw = raw[1:].strip()

        # name(param1, param2) return_type 파싱
        m = re.match(r"([A-Za-z0-9_]+)\s*\((.*?)\)\s*(.*)", raw)
        op_id = _gen_staruml_id()
        parameters: List[Dict[str, Any]] = []

        if m:
            name, param_str, ret_type = m.groups()
            name = name.strip()
            if param_str.strip():
                for p in param_str.split(","):
                    p = p.strip().replace("~", "<", 1).replace("~", ">")
                    if not p:
                        continue
                    if ":" in p:
                        pname, ptype = [x.strip() for x in p.split(":", 1)]
                    else:
                        parts = p.split()
                        if len(parts) >= 2:
                            ptype, pname = parts[0], parts[1]
                        else:
                            pname, ptype = parts[0], ""
                    param_elem: Dict[str, Any] = {
                        "_type": "UMLParameter",
                        "_id": _gen_staruml_id(),
                        "_parent": {"$ref": op_id},
                        "name": pname,
                        "direction": "in",
                    }
                    if ptype:
                        param_elem["type"] = ptype
                    parameters.append(param_elem)

            if ret_type.strip():
                ret_clean = ret_type.strip().replace("~", "<", 1).replace("~", ">")
                parameters.append({
                    "_type": "UMLParameter",
                    "_id": _gen_staruml_id(),
                    "_parent": {"$ref": op_id},
                    "name": "return",
                    "type": ret_clean,
                    "direction": "return",
                })
        else:
            name = raw.rstrip("()").strip()

        return {
            "_type": "UMLOperation",
            "_id": op_id,
            "_parent": {"$ref": parent_id},
            "name": name,
            "visibility": vis,
            "parameters": parameters,
        }

    def _convert_class_diagram(self, lines: List[str], raw_code: str) -> str:
        classes: Dict[str, Dict[str, Any]] = {}
        relations: List[Dict[str, Any]] = []

        rel_pattern = re.compile(
            r'^\s*([A-Za-z0-9_]+)\s*(?:\"([^\"]*)\")?\s*'
            r'([<*o]?\|?--\|?[*o>]?|[<*o]?\|?\.\.\|?[*o>]?|<--|-->|--|\.\.>|<\.\.|\.\.)\s*'
            r'(?:\"([^\"]*)\")?\s*([A-Za-z0-9_]+)'
            r'(?:\s*:\s*(.*))?$'
        )

        curr_class: Optional[str] = None
        for line in lines[1:]:
            line_str = line.strip()
            if not line_str:
                continue

            if line_str.startswith("class ") and "{" in line_str:
                cname = line_str.replace("class ", "").split("{")[0].strip()
                curr_class = cname
                if cname not in classes:
                    classes[cname] = {"name": cname, "attrs": [], "methods": [], "elem_id": _gen_staruml_id(), "view_id": _gen_staruml_id()}
                continue
            elif line_str == "}":
                curr_class = None
                continue

            if curr_class:
                if "(" in line_str and ")" in line_str:
                    classes[curr_class]["methods"].append(line_str)
                else:
                    classes[curr_class]["attrs"].append(line_str)
                continue

            # 클래스 간 관계 파싱 (User "1" --> "*" Order : places 등 모든 표기 지원)
            rel_m = rel_pattern.match(line_str)
            if rel_m:
                c1, card1, rel_type, card2, c2, lbl = rel_m.groups()
                for c in (c1, c2):
                    if c not in classes:
                        classes[c] = {"name": c, "attrs": [], "methods": [], "elem_id": _gen_staruml_id(), "view_id": _gen_staruml_id()}

                clean_lbl = (lbl or "").strip().strip('"\'')
                clean_card1 = (card1 or "").strip()
                clean_card2 = (card2 or "").strip()

                # 관계 종류 및 방향 판별
                is_gen = "<|--" in rel_type or "--|>" in rel_type
                is_real = "<|.." in rel_type or "..|>" in rel_type
                is_dep = rel_type in ("..>", "<..", "..")

                if "<|--" in rel_type or "<|.." in rel_type or "<.." in rel_type:
                    src_name, tgt_name = c2, c1
                    card_src, card_tgt = clean_card2, clean_card1
                else:
                    src_name, tgt_name = c1, c2
                    card_src, card_tgt = clean_card1, clean_card2

                agg1 = "none"
                agg2 = "none"
                if "*--" in rel_type:
                    agg1 = "composite"
                elif "--*" in rel_type:
                    agg2 = "composite"
                elif "o--" in rel_type:
                    agg1 = "shared"
                elif "--o" in rel_type:
                    agg2 = "shared"

                nav2 = "-->" in rel_type or "<--" in rel_type or agg1 != "none" or agg2 != "none"

                relations.append({
                    "source": src_name,
                    "target": tgt_name,
                    "type": rel_type,
                    "label": clean_lbl,
                    "card1": card_src,
                    "card2": card_tgt,
                    "is_gen": is_gen,
                    "is_real": is_real,
                    "is_dep": is_dep,
                    "agg1": agg1,
                    "agg2": agg2,
                    "nav2": nav2,
                    "elem_id": _gen_staruml_id(),
                    "view_id": _gen_staruml_id(),
                    "end1_id": _gen_staruml_id(),
                    "end2_id": _gen_staruml_id(),
                })
                continue

            if line_str.startswith("class "):
                cname = line_str.replace("class ", "").strip()
                if cname not in classes:
                    classes[cname] = {"name": cname, "attrs": [], "methods": [], "elem_id": _gen_staruml_id(), "view_id": _gen_staruml_id()}

        if not classes:
            classes["DefaultClass"] = {"name": "Model", "attrs": [], "methods": [], "elem_id": _gen_staruml_id(), "view_id": _gen_staruml_id()}

        class_list = list(classes.values())
        cols = max(1, int(len(class_list) ** 0.5 + 0.5))
        for idx, cls in enumerate(class_list):
            row = idx // cols
            col = idx % cols
            cls["x"] = 80 + col * 280
            cls["y"] = 80 + row * 220
            cls["width"] = 230
            cls["height"] = max(110, 60 + (len(cls["attrs"]) + len(cls["methods"])) * 22)

        owned_elements = []
        owned_views = []

        for cls in class_list:
            uml_attrs = [self._parse_attribute(attr, cls["elem_id"]) for attr in cls["attrs"]]
            uml_ops = [self._parse_operation(meth, cls["elem_id"]) for meth in cls["methods"]]

            cls_owned_elements: List[Dict[str, Any]] = []

            for rel in [r for r in relations if r["source"] == cls["name"]]:
                target_cls = classes.get(rel["target"])
                if target_cls:
                    if rel["is_gen"]:
                        rel_elem = {
                            "_type": "UMLGeneralization",
                            "_id": rel["elem_id"],
                            "_parent": {"$ref": cls["elem_id"]},
                            "source": {"$ref": cls["elem_id"]},
                            "target": {"$ref": target_cls["elem_id"]},
                        }
                    elif rel["is_real"]:
                        rel_elem = {
                            "_type": "UMLInterfaceRealization",
                            "_id": rel["elem_id"],
                            "_parent": {"$ref": cls["elem_id"]},
                            "source": {"$ref": cls["elem_id"]},
                            "target": {"$ref": target_cls["elem_id"]},
                        }
                    elif rel["is_dep"]:
                        rel_elem = {
                            "_type": "UMLDependency",
                            "_id": rel["elem_id"],
                            "_parent": {"$ref": cls["elem_id"]},
                            "name": rel["label"],
                            "source": {"$ref": cls["elem_id"]},
                            "target": {"$ref": target_cls["elem_id"]},
                        }
                    else:
                        end1: Dict[str, Any] = {
                            "_type": "UMLAssociationEnd",
                            "_id": rel["end1_id"],
                            "_parent": {"$ref": rel["elem_id"]},
                            "reference": {"$ref": cls["elem_id"]},
                            "aggregation": rel["agg1"],
                        }
                        if rel["card1"]:
                            end1["multiplicity"] = rel["card1"]

                        end2: Dict[str, Any] = {
                            "_type": "UMLAssociationEnd",
                            "_id": rel["end2_id"],
                            "_parent": {"$ref": rel["elem_id"]},
                            "reference": {"$ref": target_cls["elem_id"]},
                            "navigable": rel["nav2"],
                            "aggregation": rel["agg2"],
                        }
                        if rel["card2"]:
                            end2["multiplicity"] = rel["card2"]

                        rel_elem = {
                            "_type": "UMLAssociation",
                            "_id": rel["elem_id"],
                            "_parent": {"$ref": cls["elem_id"]},
                            "name": rel["label"],
                            "end1": end1,
                            "end2": end2,
                        }
                    cls_owned_elements.append(rel_elem)

            uml_cls = {
                "_type": "UMLClass",
                "_id": cls["elem_id"],
                "_parent": {"$ref": self.model_id},
                "name": cls["name"],
                "attributes": uml_attrs,
                "operations": uml_ops,
                "ownedElements": cls_owned_elements,
            }
            owned_elements.append(uml_cls)

            cls_view = {
                "_type": "UMLClassView",
                "_id": cls["view_id"],
                "_parent": {"$ref": self.diagram_id},
                "model": {"$ref": cls["elem_id"]},
                "left": cls["x"],
                "top": cls["y"],
                "width": cls["width"],
                "height": cls["height"],
                "nameLabel": {"_type": "LabelView", "_id": _gen_staruml_id(), "_parent": {"$ref": cls["view_id"]}, "text": cls["name"]},
            }
            owned_views.append(cls_view)

        for rel in relations:
            src = classes.get(rel["source"])
            tgt = classes.get(rel["target"])
            if src and tgt:
                if rel["is_gen"]:
                    view_type = "UMLGeneralizationView"
                elif rel["is_real"]:
                    view_type = "UMLInterfaceRealizationView"
                elif rel["is_dep"]:
                    view_type = "UMLDependencyView"
                else:
                    view_type = "UMLAssociationView"

                rel_view: Dict[str, Any] = {
                    "_type": view_type,
                    "_id": rel["view_id"],
                    "_parent": {"$ref": self.diagram_id},
                    "model": {"$ref": rel["elem_id"]},
                    "tail": {"$ref": src["view_id"]},
                    "head": {"$ref": tgt["view_id"]},
                    "lineStyle": 1,
                    "points": f"{src['x'] + src['width']//2}:{src['y'] + src['height']//2};{tgt['x'] + tgt['width']//2}:{tgt['y'] + tgt['height']//2}",
                }
                if rel["label"]:
                    rel_view["nameLabel"] = {
                        "_type": "EdgeLabelView",
                        "_id": _gen_staruml_id(),
                        "_parent": {"$ref": rel["view_id"]},
                        "model": {"$ref": rel["elem_id"]},
                        "text": rel["label"],
                        "visible": True,
                    }
                if not (rel["is_gen"] or rel["is_real"] or rel["is_dep"]):
                    if rel["card1"]:
                        rel_view["tailNameLabel"] = {
                            "_type": "EdgeLabelView",
                            "_id": _gen_staruml_id(),
                            "_parent": {"$ref": rel["view_id"]},
                            "model": {"$ref": rel["end1_id"]},
                            "text": rel["card1"],
                            "visible": True,
                        }
                    if rel["card2"]:
                        rel_view["headNameLabel"] = {
                            "_type": "EdgeLabelView",
                            "_id": _gen_staruml_id(),
                            "_parent": {"$ref": rel["view_id"]},
                            "model": {"$ref": rel["end2_id"]},
                            "text": rel["card2"],
                            "visible": True,
                        }
                owned_views.append(rel_view)

        diagram = {
            "_type": "UMLClassDiagram",
            "_id": self.diagram_id,
            "_parent": {"$ref": self.model_id},
            "name": self.title,
            "visible": True,
            "defaultDiagram": True,
            "ownedViews": owned_views,
        }

        project = {
            "_type": "Project",
            "_id": self.project_id,
            "name": self.title,
            "documentation": f"Generated from MADO (Multi-Agent Debate & Orchestration)\n\nOriginal Mermaid Code:\n```mermaid\n{raw_code}\n```",
            "ownedElements": [
                {
                    "_type": "UMLModel",
                    "_id": self.model_id,
                    "_parent": {"$ref": self.project_id},
                    "name": "ClassModel",
                    "ownedElements": [diagram] + owned_elements,
                }
            ],
        }
        return json.dumps(project, indent=2, ensure_ascii=False)

    def _convert_sequence_diagram(self, lines: List[str], raw_code: str) -> str:
        participants: Dict[str, Dict[str, Any]] = {}
        messages: List[Dict[str, Any]] = []

        part_pattern = re.compile(r'^\s*(?:participant|actor)\s+([A-Za-z0-9_]+)(?:\s+as\s+[\"\']?([^\r\n\"\']+)[\"\']?)?')
        msg_pattern = re.compile(r'^\s*([A-Za-z0-9_]+)\s*(-->>|->>|-->|->)\s*([A-Za-z0-9_]+)\s*:\s*(.+)')

        for line in lines[1:]:
            line_str = line.strip()
            if not line_str or line_str.startswith(("autonumber", "box")) or line_str == "end":
                continue

            p_m = part_pattern.match(line_str)
            if p_m and not msg_pattern.match(line_str):
                p_id, p_alias = p_m.groups()
                p_label = (p_alias or p_id).strip().strip('"\'')
                if p_id not in participants:
                    participants[p_id] = {
                        "id": p_id,
                        "name": p_label,
                        "elem_id": _gen_staruml_id(),
                        "view_id": _gen_staruml_id(),
                        "line_part_id": _gen_staruml_id(),
                    }
                continue

            m_m = msg_pattern.match(line_str)
            if m_m:
                s_id, arrow, t_id, text = m_m.groups()
                for p in (s_id, t_id):
                    if p not in participants:
                        participants[p] = {
                            "id": p,
                            "name": p,
                            "elem_id": _gen_staruml_id(),
                            "view_id": _gen_staruml_id(),
                            "line_part_id": _gen_staruml_id(),
                        }
                messages.append({
                    "source": s_id,
                    "target": t_id,
                    "text": text.strip(),
                    "arrow": arrow,
                    "elem_id": _gen_staruml_id(),
                    "view_id": _gen_staruml_id(),
                })

        if not participants:
            participants["Participant1"] = {
                "id": "Participant1",
                "name": "Actor",
                "elem_id": _gen_staruml_id(),
                "view_id": _gen_staruml_id(),
                "line_part_id": _gen_staruml_id(),
            }

        part_list = list(participants.values())
        interaction_id = _gen_staruml_id()

        start_x = 80
        spacing_x = 220
        head_height = 40
        line_length = max(450, 160 + len(messages) * 55)

        for idx, p in enumerate(part_list):
            p["x"] = start_x + idx * spacing_x
            p["y"] = 40
            p["width"] = 140
            p["height"] = line_length

        owned_views = []
        lifelines = []

        for p in part_list:
            lifeline_elem = {
                "_type": "UMLLifeline",
                "_id": p["elem_id"],
                "_parent": {"$ref": interaction_id},
                "name": p["name"],
                "isMultiInstance": False,
            }
            lifelines.append(lifeline_elem)

            name_comp_id = _gen_staruml_id()
            st_label_id = _gen_staruml_id()
            n_label_id = _gen_staruml_id()
            ns_label_id = _gen_staruml_id()
            p_label_id = _gen_staruml_id()

            name_comp_view = {
                "_type": "UMLNameCompartmentView",
                "_id": name_comp_id,
                "_parent": {"$ref": p["view_id"]},
                "model": {"$ref": p["elem_id"]},
                "subViews": [
                    {
                        "_type": "LabelView",
                        "_id": st_label_id,
                        "_parent": {"$ref": name_comp_id},
                        "visible": False,
                    },
                    {
                        "_type": "LabelView",
                        "_id": n_label_id,
                        "_parent": {"$ref": name_comp_id},
                        "text": p["name"],
                    },
                    {
                        "_type": "LabelView",
                        "_id": ns_label_id,
                        "_parent": {"$ref": name_comp_id},
                        "visible": False,
                    },
                    {
                        "_type": "LabelView",
                        "_id": p_label_id,
                        "_parent": {"$ref": name_comp_id},
                        "visible": False,
                    },
                ],
                "stereotypeLabel": {"$ref": st_label_id},
                "nameLabel": {"$ref": n_label_id},
                "namespaceLabel": {"$ref": ns_label_id},
                "propertyLabel": {"$ref": p_label_id},
            }

            line_part_view = {
                "_type": "UMLLinePartView",
                "_id": p["line_part_id"],
                "_parent": {"$ref": p["view_id"]},
                "model": {"$ref": p["elem_id"]},
                "left": p["x"] + p["width"] // 2,
                "top": p["y"] + head_height,
                "width": 1,
                "height": line_length - head_height,
            }

            lifeline_view = {
                "_type": "UMLSeqLifelineView",
                "_id": p["view_id"],
                "_parent": {"$ref": self.diagram_id},
                "model": {"$ref": p["elem_id"]},
                "subViews": [
                    name_comp_view,
                    line_part_view,
                ],
                "left": p["x"],
                "top": p["y"],
                "width": p["width"],
                "height": p["height"],
                "nameCompartment": {"$ref": name_comp_id},
                "linePart": {"$ref": p["line_part_id"]},
            }
            owned_views.append(lifeline_view)

        msg_elements = []
        start_y = 120
        step_y = 50

        for idx, msg in enumerate(messages):
            src = participants.get(msg["source"])
            tgt = participants.get(msg["target"])
            if src and tgt:
                is_reply = "--" in msg["arrow"]
                msg_elem = {
                    "_type": "UMLMessage",
                    "_id": msg["elem_id"],
                    "_parent": {"$ref": interaction_id},
                    "name": msg["text"],
                    "source": {"$ref": src["elem_id"]},
                    "target": {"$ref": tgt["elem_id"]},
                    "messageSort": "reply" if is_reply else "synchCall",
                }
                msg_elements.append(msg_elem)

                src_cx = src["x"] + src["width"] // 2
                tgt_cx = tgt["x"] + tgt["width"] // 2
                curr_y = start_y + idx * step_y

                if src["id"] == tgt["id"]:
                    points_str = f"{src_cx}:{curr_y};{src_cx + 45}:{curr_y};{src_cx + 45}:{curr_y + 25};{src_cx}:{curr_y + 25}"
                else:
                    points_str = f"{src_cx}:{curr_y};{tgt_cx}:{curr_y}"

                n_label_id = _gen_staruml_id()
                st_label_id = _gen_staruml_id()
                p_label_id = _gen_staruml_id()
                act_id = _gen_staruml_id()

                msg_view = {
                    "_type": "UMLSeqMessageView",
                    "_id": msg["view_id"],
                    "_parent": {"$ref": self.diagram_id},
                    "model": {"$ref": msg["elem_id"]},
                    "subViews": [
                        {
                            "_type": "EdgeLabelView",
                            "_id": n_label_id,
                            "_parent": {"$ref": msg["view_id"]},
                            "model": {"$ref": msg["elem_id"]},
                            "text": msg["text"],
                            "alpha": 1.5707963267948966,
                            "distance": 10,
                            "edgePosition": 1,
                        },
                        {
                            "_type": "EdgeLabelView",
                            "_id": st_label_id,
                            "_parent": {"$ref": msg["view_id"]},
                            "model": {"$ref": msg["elem_id"]},
                            "visible": False,
                            "alpha": 1.5707963267948966,
                            "distance": 25,
                            "edgePosition": 1,
                        },
                        {
                            "_type": "EdgeLabelView",
                            "_id": p_label_id,
                            "_parent": {"$ref": msg["view_id"]},
                            "model": {"$ref": msg["elem_id"]},
                            "visible": False,
                            "alpha": -1.5707963267948966,
                            "distance": 10,
                            "edgePosition": 1,
                        },
                        {
                            "_type": "UMLActivationView",
                            "_id": act_id,
                            "_parent": {"$ref": msg["view_id"]},
                            "model": {"$ref": msg["elem_id"]},
                            "visible": not is_reply,
                        },
                    ],
                    "head": {"$ref": tgt["line_part_id"]},
                    "tail": {"$ref": src["line_part_id"]},
                    "points": points_str,
                    "nameLabel": {"$ref": n_label_id},
                    "stereotypeLabel": {"$ref": st_label_id},
                    "propertyLabel": {"$ref": p_label_id},
                    "activation": {"$ref": act_id},
                }
                owned_views.append(msg_view)

        diagram = {
            "_type": "UMLSequenceDiagram",
            "_id": self.diagram_id,
            "_parent": {"$ref": interaction_id},
            "name": self.title,
            "visible": True,
            "defaultDiagram": True,
            "showSequenceNumber": True,
            "showSignature": True,
            "showActivation": True,
            "ownedViews": owned_views,
        }

        interaction = {
            "_type": "UMLInteraction",
            "_id": interaction_id,
            "_parent": {"$ref": self.model_id},
            "name": "Interaction",
            "ownedElements": [diagram],
            "participants": lifelines,
            "messages": msg_elements,
        }

        project = {
            "_type": "Project",
            "_id": self.project_id,
            "name": self.title,
            "documentation": f"Generated from MADO (Multi-Agent Debate & Orchestration)\n\nOriginal Mermaid Code:\n```mermaid\n{raw_code}\n```",
            "ownedElements": [
                {
                    "_type": "UMLModel",
                    "_id": self.model_id,
                    "_parent": {"$ref": self.project_id},
                    "name": "SequenceModel",
                    "ownedElements": [interaction],
                }
            ],
        }
        return json.dumps(project, indent=2, ensure_ascii=False)


def convert_mermaid_to_staruml_mdj(title: str, mermaid_code: str) -> str:
    """Mermaid 다이어그램을 StarUML .mdj JSON 포맷으로 변환합니다."""
    converter = MermaidToStarUMLConverter(title=title)
    return converter.convert(mermaid_code)


def _clean_title_for_export(title: str, default: str = "architecture_diagram") -> str:
    """제목을 파일 이름으로 쓸 수 있게 다듬습니다 (HTML 안의 저장 버튼용)."""
    cleaned = "".join(
        c for c in (title or "") if c.isalnum() or c in ("-", "_", " ")
    ).strip().replace(" ", "_")
    return cleaned or default


# Mermaid 렌더러를 내려받는 곳. 여기에 닿지 못하면 다이어그램을 그릴 수 없으므로,
# 가능하면 브라우저가 이미 그려 둔 SVG 를 함께 실어 보내 이 주소를 아예 쓰지
# 않게 합니다 (`svg_content`).
MERMAID_CDN_URL = "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"


def generate_mermaid_standalone_html(
    title: str,
    mermaid_code: str,
    svg_content: Optional[str] = None,
) -> str:
    """줌/패닝, 테마 토글, 다운로드 컨트롤이 포함된 단일 독립형 HTML 문서를 생성합니다.

    `svg_content` 에 **이미 렌더링된 SVG** 를 주면 그것을 문서에 그대로 심고
    CDN 스크립트를 아예 넣지 않습니다. 그래야 이름값을 합니다 — 예전에는
    파일 하나에 담겼을 뿐 열 때마다 인터넷으로 렌더러를 받아 와야 했고,
    폐쇄망이나 비행기 안에서 열면 흰 화면만 나왔습니다.

    SVG 를 못 구하면 CDN 을 쓰는 예전 방식으로 돌아가되, 렌더러가 없을 때
    **원본 소스를 읽을 수 있게** 남깁니다.
    """
    safe_title = html.escape(title or "Architecture Diagram")
    raw_mermaid = (mermaid_code or "").strip()
    escaped_mermaid = html.escape(raw_mermaid)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 파일명은 JS 문자열 리터럴 안으로 들어갑니다. HTML 이스케이프한 제목을 그대로
    # 쓰면 따옴표가 `&#x27;` 로 남아 파일 이름에 그대로 찍힙니다.
    js_filename = json.dumps(_clean_title_for_export(title))

    embedded_svg = (svg_content or "").strip()
    has_embedded_svg = bool(embedded_svg)

    # 렌더러가 필요 없으면 부르지 않습니다. 없는 주소를 붙들고 있을 이유가 없고,
    # 폐쇄망에서는 그 요청이 타임아웃까지 문서를 붙잡습니다.
    mermaid_script_tag = (
        ""
        if has_embedded_svg
        else f'    <!-- Mermaid CDN (SVG 를 심지 못했을 때만 씁니다) -->\n'
             f'    <script src="{MERMAID_CDN_URL}"></script>'
    )

    # Mermaid 태그 안의 원본은 **이스케이프해서** 넣습니다. Mermaid 는 요소의
    # textContent 를 읽으므로 실체는 그대로 복원되고, 그동안 `<`, `&` 가 들어간
    # 다이어그램(클래스 다이어그램의 제네릭, 라벨 안의 태그)이 HTML 파서에
    # 먹혀 통째로 사라지던 것이 막힙니다.
    svg_container = embedded_svg or f'<div class="mermaid">{escaped_mermaid}</div>'
    js_has_svg = "true" if has_embedded_svg else "false"

    html_template = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{safe_title} - MADO Diagram Export</title>
{mermaid_script_tag}
    <style>
        :root {{
            --bg-color: #0f172a;
            --surface-color: #1e293b;
            --border-color: #334155;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --accent: #6366f1;
            --accent-hover: #4f46e5;
            --diagram-bg: #f8fafc;
            --diagram-border: #cbd5e1;
        }}
        [data-theme="light"] {{
            --bg-color: #f1f5f9;
            --surface-color: #ffffff;
            --border-color: #e2e8f0;
            --text-main: #0f172a;
            --text-muted: #64748b;
            --accent: #4f46e5;
            --accent-hover: #4338ca;
            --diagram-bg: #ffffff;
            --diagram-border: #cbd5e1;
        }}
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            transition: background-color 0.2s ease, color 0.2s ease;
        }}
        header {{
            background-color: var(--surface-color);
            border-bottom: 1px solid var(--border-color);
            padding: 12px 24px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 12px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}
        .header-title {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        .header-title h1 {{
            font-size: 1.1rem;
            font-weight: 700;
            color: var(--text-main);
        }}
        .badge {{
            background-color: var(--accent);
            color: #ffffff;
            font-size: 0.75rem;
            font-weight: 600;
            padding: 2px 8px;
            border-radius: 9999px;
            text-transform: uppercase;
        }}
        .header-meta {{
            font-size: 0.75rem;
            color: var(--text-muted);
        }}
        .toolbar {{
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }}
        button {{
            background-color: var(--surface-color);
            color: var(--text-main);
            border: 1px solid var(--border-color);
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 0.8rem;
            font-weight: 500;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            transition: all 0.15s ease;
        }}
        button:hover {{
            background-color: var(--border-color);
        }}
        button.primary {{
            background-color: var(--accent);
            color: #ffffff;
            border-color: var(--accent);
        }}
        button.primary:hover {{
            background-color: var(--accent-hover);
        }}
        main {{
            flex: 1;
            display: flex;
            flex-direction: column;
            padding: 16px;
            position: relative;
            overflow: hidden;
        }}
        .view-container {{
            flex: 1;
            display: flex;
            background-color: var(--surface-color);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            position: relative;
            overflow: hidden;
        }}
        #diagram-view {{
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            background-color: var(--diagram-bg);
            border: 1px solid var(--diagram-border);
            margin: 12px;
            border-radius: 8px;
            overflow: hidden;
            position: relative;
            cursor: grab;
            user-select: none;
        }}
        #diagram-view:active {{
            cursor: grabbing;
        }}
        #diagram-content {{
            transform-origin: center center;
            transition: transform 0.05s ease-out;
            padding: 30px;
            display: inline-flex;
            justify-content: center;
            align-items: center;
            will-change: transform;
            /* 이 판은 테마와 상관없이 늘 밝습니다. 색을 물려받게 두면 다크 테마의
               흰 글자가 흰 배경에 얹혀, 렌더링이 실패했을 때 아무것도 안 보입니다
               (증상: "흰 백지만 나온다"). */
            color: #0f172a;
        }}
        #diagram-content .mermaid {{
            /* 렌더러가 없을 때 이 자리에 남는 것은 원본 Mermaid 소스입니다.
               읽을 수 있게 둡니다. */
            white-space: pre-wrap;
            text-align: left;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 0.8rem;
            line-height: 1.55;
        }}
        #render-warning {{
            display: none;
            margin: 0 12px 12px;
            padding: 10px 14px;
            border: 1px solid #f59e0b;
            border-left-width: 4px;
            border-radius: 8px;
            background-color: rgba(245, 158, 11, 0.12);
            color: #fbbf24;
            font-size: 0.78rem;
            line-height: 1.5;
        }}
        #diagram-content svg {{
            display: block;
            max-width: none !important;
            height: auto;
        }}
        #code-view {{
            display: none;
            flex: 1;
            padding: 20px;
            overflow: auto;
            background-color: #090d16;
            color: #38bdf8;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 0.85rem;
            line-height: 1.5;
            white-space: pre-wrap;
        }}
        .floating-controls {{
            position: absolute;
            bottom: 24px;
            right: 24px;
            display: flex;
            gap: 6px;
            background-color: rgba(15, 23, 42, 0.85);
            backdrop-filter: blur(8px);
            padding: 6px;
            border-radius: 8px;
            border: 1px solid var(--border-color);
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            z-index: 10;
        }}
        .floating-controls button {{
            background: transparent;
            border: none;
            color: #f8fafc;
            padding: 6px 10px;
            font-size: 0.85rem;
        }}
        .floating-controls button:hover {{
            background-color: rgba(255, 255, 255, 0.15);
        }}
        .toast {{
            position: fixed;
            bottom: 30px;
            left: 50%;
            transform: translateX(-50%);
            background-color: #10b981;
            color: #ffffff;
            padding: 8px 16px;
            border-radius: 8px;
            font-size: 0.85rem;
            font-weight: 600;
            box-shadow: 0 4px 12px rgba(0,0,0,0.25);
            opacity: 0;
            transition: opacity 0.2s ease, transform 0.2s ease;
            pointer-events: none;
            z-index: 100;
        }}
        .toast.show {{
            opacity: 1;
            transform: translateX(-50%) translateY(-5px);
        }}
        footer {{
            padding: 8px 24px;
            font-size: 0.7rem;
            color: var(--text-muted);
            text-align: center;
            border-top: 1px solid var(--border-color);
            background-color: var(--surface-color);
        }}
    </style>
</head>
<body>
    <header>
        <div class="header-title">
            <span class="badge">Mermaid</span>
            <h1>{safe_title}</h1>
            <span class="header-meta">{now_str}</span>
        </div>
        <div class="toolbar">
            <button id="btn-toggle-view">📝 소스 보기</button>
            <button id="btn-copy-img" class="primary">📋 이미지 복사</button>
            <button id="btn-download-png">🖼️ PNG 저장</button>
            <button id="btn-download-svg">📐 SVG 저장</button>
            <button id="btn-theme">🌓 테마</button>
        </div>
    </header>

    <main>
        <div id="render-warning"></div>
        <div class="view-container">
            <div id="diagram-view">
                <div id="diagram-content">
                    {svg_container}
                </div>
            </div>
            <pre id="code-view"><code>{escaped_mermaid}</code></pre>
        </div>

        <div class="floating-controls" id="zoom-controls">
            <button id="btn-zoom-in" title="확대 (Zoom In)">➕ 확대</button>
            <button id="btn-zoom-reset" title="원래 크기 (100%)">100%</button>
            <button id="btn-zoom-out" title="축소 (Zoom Out)">➖ 축소</button>
            <button id="btn-zoom-fit" title="화면에 맞춤">⛶ 맞춤</button>
        </div>
    </main>

    <div id="toast" class="toast">클립보드에 복사되었습니다!</div>

    <footer>
        MADO (Multi-Agent Debate & Orchestration Platform) 산출물
    </footer>

    <script>
        // 렌더러 준비. **여기서 예외가 새면 아래의 툴바·줌·저장이 전부 죽습니다**
        // (예전에는 CDN 에 닿지 못하면 `mermaid is not defined` 한 줄로 스크립트가
        // 통째로 멈춰, 화면도 버튼도 아무것도 동작하지 않았습니다).
        const HAS_EMBEDDED_SVG = {js_has_svg};

        function showRenderWarning(msg) {{
            const box = document.getElementById('render-warning');
            if (!box) return;
            box.textContent = msg;
            box.style.display = 'block';
        }}

        if (!HAS_EMBEDDED_SVG) {{
            if (typeof mermaid === 'undefined') {{
                showRenderWarning(
                    '⚠️ Mermaid 렌더러를 불러오지 못했습니다 (인터넷 연결이 없거나 CDN 이 차단된 환경). '
                    + '아래에는 다이어그램 대신 원본 소스를 그대로 표시합니다. '
                    + '그림이 필요하면 인터넷이 되는 곳에서 다시 열거나, 앱에서 PNG/SVG 로 내려받으세요.'
                );
            }} else {{
                try {{
                    mermaid.initialize({{ startOnLoad: true, theme: 'default', securityLevel: 'loose' }});
                }} catch (err) {{
                    showRenderWarning('⚠️ Mermaid 초기화에 실패했습니다: ' + err);
                }}
            }}
        }}

        let currentScale = 1.0;
        let translateX = 0;
        let translateY = 0;
        let isDragging = false;
        let startX, startY;

        const diagramView = document.getElementById('diagram-view');
        const diagramContent = document.getElementById('diagram-content');
        const codeView = document.getElementById('code-view');
        const toast = document.getElementById('toast');

        function showToast(msg) {{
            toast.textContent = msg;
            toast.classList.add('show');
            setTimeout(() => toast.classList.remove('show'), 2000);
        }}

        function applyTransform() {{
            diagramContent.style.transform = `translate(${{translateX}}px, ${{translateY}}px) scale(${{currentScale}})`;
        }}

        function fitDiagram() {{
            const svg = diagramContent.querySelector('svg');
            if (!svg) {{
                currentScale = 1.0;
                translateX = 0;
                translateY = 0;
                applyTransform();
                return;
            }}
            const viewRect = diagramView.getBoundingClientRect();
            let svgW = svg.viewBox?.baseVal?.width || svg.clientWidth || parseFloat(svg.getAttribute('width')) || 800;
            let svgH = svg.viewBox?.baseVal?.height || svg.clientHeight || parseFloat(svg.getAttribute('height')) || 600;

            const padX = 60;
            const padY = 60;
            const availableW = Math.max(100, viewRect.width - padX);
            const availableH = Math.max(100, viewRect.height - padY);

            const scaleX = availableW / svgW;
            const scaleY = availableH / svgH;
            currentScale = Math.min(Math.max(Math.min(scaleX, scaleY), 0.1), 2.5);
            translateX = 0;
            translateY = 0;
            applyTransform();
        }}

        // Zoom Controls
        document.getElementById('btn-zoom-in').addEventListener('click', () => {{
            currentScale = Math.min(currentScale * 1.25, 10.0);
            applyTransform();
        }});
        document.getElementById('btn-zoom-out').addEventListener('click', () => {{
            currentScale = Math.max(currentScale / 1.25, 0.1);
            applyTransform();
        }});
        document.getElementById('btn-zoom-reset').addEventListener('click', () => {{
            currentScale = 1.0;
            translateX = 0;
            translateY = 0;
            applyTransform();
        }});
        document.getElementById('btn-zoom-fit').addEventListener('click', () => {{
            fitDiagram();
        }});

        // Mouse Wheel Zoom
        diagramView.addEventListener('wheel', (e) => {{
            e.preventDefault();
            const delta = e.deltaY > 0 ? 0.85 : 1.15;
            currentScale = Math.min(Math.max(currentScale * delta, 0.1), 10.0);
            applyTransform();
        }}, {{ passive: false }});

        // Drag to Pan
        diagramView.addEventListener('mousedown', (e) => {{
            if (e.target.closest('button')) return;
            isDragging = true;
            startX = e.clientX - translateX;
            startY = e.clientY - translateY;
        }});
        window.addEventListener('mousemove', (e) => {{
            if (!isDragging) return;
            translateX = e.clientX - startX;
            translateY = e.clientY - startY;
            applyTransform();
        }});
        window.addEventListener('mouseup', () => isDragging = false);

        // View Toggle (Diagram vs Code)
        let isCodeMode = false;
        const btnToggleView = document.getElementById('btn-toggle-view');
        const zoomControls = document.getElementById('zoom-controls');

        btnToggleView.addEventListener('click', () => {{
            isCodeMode = !isCodeMode;
            if (isCodeMode) {{
                diagramView.style.display = 'none';
                codeView.style.display = 'block';
                zoomControls.style.display = 'none';
                btnToggleView.textContent = '📊 다이어그램 보기';
            }} else {{
                diagramView.style.display = 'flex';
                codeView.style.display = 'none';
                zoomControls.style.display = 'flex';
                btnToggleView.textContent = '📝 소스 보기';
                setTimeout(fitDiagram, 50);
            }}
        }});

        // Theme Toggle
        let isLight = false;
        document.getElementById('btn-theme').addEventListener('click', () => {{
            isLight = !isLight;
            document.documentElement.setAttribute('data-theme', isLight ? 'light' : 'dark');
        }});

        // Initial Auto Fit after Mermaid finishes rendering
        setTimeout(fitDiagram, 400);

        // SVG / PNG Extraction Helper
        function sanitizeSvgForCanvas(svgElement) {{
            const clone = svgElement.cloneNode(true);
            clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
            clone.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink');

            clone.querySelectorAll('style').forEach(function(styleTag) {{
                let css = styleTag.textContent || '';
                css = css.replace(/@import\\s+url\\([^)]+\\);?/gi, '');
                css = css.replace(/@font-face[^{{]*\\{{[^}}]*\\}}/gi, '');
                styleTag.textContent = css;
            }});

            clone.querySelectorAll('foreignObject').forEach(function(fo) {{
                const x = parseFloat(fo.getAttribute('x') || 0);
                const y = parseFloat(fo.getAttribute('y') || 0);
                const width = parseFloat(fo.getAttribute('width') || 0);
                const height = parseFloat(fo.getAttribute('height') || 0);
                const textContent = (fo.textContent || '').trim();
                if (!textContent) {{ fo.remove(); return; }}

                const textElem = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                textElem.setAttribute('x', (x + width / 2).toString());
                textElem.setAttribute('y', (y + height / 2).toString());
                textElem.setAttribute('text-anchor', 'middle');
                textElem.setAttribute('dominant-baseline', 'central');
                textElem.setAttribute('alignment-baseline', 'central');
                textElem.setAttribute('fill', '#0f172a');
                textElem.setAttribute('font-family', '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif');
                textElem.setAttribute('font-size', '13px');
                textElem.setAttribute('font-weight', '500');

                const lines = textContent.split(/\\r?\\n/).map(function(l) {{ return l.trim(); }}).filter(Boolean);
                if (lines.length > 1) {{
                    const lineHeight = 16;
                    const startY = y + height / 2 - ((lines.length - 1) * lineHeight) / 2;
                    textElem.textContent = '';
                    lines.forEach(function(line, idx) {{
                        const tspan = document.createElementNS('http://www.w3.org/2000/svg', 'tspan');
                        tspan.setAttribute('x', (x + width / 2).toString());
                        tspan.setAttribute('y', (startY + idx * lineHeight).toString());
                        tspan.textContent = line;
                        textElem.appendChild(tspan);
                    }});
                }} else {{
                    textElem.textContent = textContent;
                }}
                if (fo.parentNode) {{ fo.parentNode.replaceChild(textElem, fo); }}
            }});
            return clone;
        }}

        function getSvgData() {{
            const svg = diagramContent.querySelector('svg');
            if (!svg) return null;
            const clone = sanitizeSvgForCanvas(svg);

            let width = svg.viewBox?.baseVal?.width || svg.clientWidth || parseFloat(svg.getAttribute('width')) || 800;
            let height = svg.viewBox?.baseVal?.height || svg.clientHeight || parseFloat(svg.getAttribute('height')) || 600;
            width = Math.max(100, Math.ceil(width));
            height = Math.max(100, Math.ceil(height));

            clone.setAttribute('width', width.toString());
            clone.setAttribute('height', height.toString());
            if (!clone.getAttribute('viewBox')) {{
                clone.setAttribute('viewBox', `0 0 ${{width}} ${{height}}`);
            }}

            const serializer = new XMLSerializer();
            let svgString = serializer.serializeToString(clone);
            if (!svgString.includes('xmlns="http://www.w3.org/2000/svg"')) {{
                svgString = svgString.replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg"');
            }}

            const rawClone = svg.cloneNode(true);
            rawClone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
            rawClone.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink');
            rawClone.setAttribute('width', width.toString());
            rawClone.setAttribute('height', height.toString());
            if (!rawClone.getAttribute('viewBox')) {{
                rawClone.setAttribute('viewBox', `0 0 ${{width}} ${{height}}`);
            }}
            let rawSvgString = serializer.serializeToString(rawClone);

            return {{ svgString: svgString, rawSvgString: rawSvgString, width: width, height: height }};
        }}

        function svgToCanvas(svgData, scale = 2) {{
            return new Promise((resolve, reject) => {{
                const base64Svg = window.btoa(unescape(encodeURIComponent(svgData.svgString)));
                const dataUrl = 'data:image/svg+xml;base64,' + base64Svg;
                const img = new Image();
                img.onload = () => {{
                    const canvas = document.createElement('canvas');
                    canvas.width = svgData.width * scale;
                    canvas.height = svgData.height * scale;
                    const ctx = canvas.getContext('2d');
                    ctx.fillStyle = '#ffffff';
                    ctx.fillRect(0, 0, canvas.width, canvas.height);
                    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
                    resolve(canvas);
                }};
                img.onerror = (err) => {{
                    reject(err);
                }};
                img.src = dataUrl;
            }});
        }}

        // Copy Image to Clipboard
        document.getElementById('btn-copy-img').addEventListener('click', async () => {{
            const svgData = getSvgData();
            if (!svgData) {{
                showToast('다이어그램 SVG 를 찾을 수 없습니다.');
                return;
            }}
            try {{
                const canvas = await svgToCanvas(svgData);
                canvas.toBlob(async (blob) => {{
                    if (blob && navigator.clipboard && window.ClipboardItem) {{
                        await navigator.clipboard.write([new ClipboardItem({{ 'image/png': blob }})]);
                        showToast('클립보드에 PNG 이미지가 복사되었습니다!');
                    }} else {{
                        showToast('이 환경에서는 이미지 복사가 지원되지 않습니다. PNG 저장을 이용하세요.');
                    }}
                }}, 'image/png');
            }} catch (err) {{
                showToast('이미지 복사 실패: ' + err);
            }}
        }});

        // Download PNG
        document.getElementById('btn-download-png').addEventListener('click', async () => {{
            const svgData = getSvgData();
            if (!svgData) {{
                showToast('다이어그램이 그려지지 않아 PNG 로 저장할 수 없습니다.');
                return;
            }}
            try {{
                const canvas = await svgToCanvas(svgData);
                canvas.toBlob((blob) => {{
                    if (!blob) return;
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = {js_filename} + '.png';
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    URL.revokeObjectURL(url);
                    showToast('PNG 파일이 다운로드되었습니다.');
                }}, 'image/png');
            }} catch (err) {{
                showToast('PNG 변환 실패: ' + err);
            }}
        }});

        // Download SVG
        document.getElementById('btn-download-svg').addEventListener('click', () => {{
            const svgData = getSvgData();
            if (!svgData) {{
                showToast('다이어그램이 그려지지 않아 SVG 로 저장할 수 없습니다.');
                return;
            }}
            const blob = new Blob([svgData.rawSvgString || svgData.svgString], {{ type: 'image/svg+xml;charset=utf-8' }});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = {js_filename} + '.svg';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            showToast('SVG 파일이 다운로드되었습니다.');
        }});
    </script>
</body>
</html>
"""
    return html_template

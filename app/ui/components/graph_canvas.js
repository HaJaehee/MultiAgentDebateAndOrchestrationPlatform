// 그래프 토론 편집 캔버스 (Vue Flow).
//
// 노드 끌기·선 잇기·삭제는 전부 브라우저 안에서 처리합니다. 서버로 가는 것은 선택이 바뀔 때,
// 처음 "변경됨" 이 될 때(깨끗한 상태에서 한 번), 그리고 저장할 때 `getGraph()` 로 읽어 갈 때뿐입니다.
// 드래그 중 이벤트마다 서버로 보내면 웹소켓이 가득 찹니다 (roster-editing.md §5.2 의 교훈).
//
// `readonly` 로 띄우면 로스터의 실행 표시가 됩니다. 끌기·잇기·선택을 끄고, 서버가 `setRun(view)` 로
// 넘기는 실행 상태(app/orchestration/graph_run.py 의 `GraphRunTracker.view()`)를 노드와 선에 칠합니다.
import { VueFlow, Handle, Position, MarkerType } from "vue-flow";
import { markRaw } from "vue";

const PORTS = {
  start: { input: false, outputs: ["out"] },
  agent: { input: true, outputs: ["out"] },
  merge: { input: true, outputs: ["out"] },
  gate: { input: true, outputs: ["yes", "no"] },
  end: { input: true, outputs: [] },
};
const KIND = { start: "START", agent: "AGENT", merge: "MERGE", gate: "GATE", end: "END" };
const CARRY = { full: "전문", digest: "요지", refs: "참조" };
const DECISION = { yes: "예", no: "아니오" };
// 노드 데이터 중 파일에 쓰는 필드 (화면용 agentName·color 등은 빼고).
const FIELDS = ["type", "label", "agent", "instruction", "question", "default", "wait", "max_visits", "sees", "plan"];

const GraphNode = {
  props: ["id", "data", "selected"],
  components: { Handle },
  setup() {
    return { Position };
  },
  computed: {
    ports() {
      return PORTS[this.data.type] || PORTS.agent;
    },
    kind() {
      return KIND[this.data.type] || this.data.type;
    },
    title() {
      const d = this.data;
      if (d.label) return d.label;
      if (d.type === "agent") return d.agentName || d.agent || "에이전트를 고르세요";
      return { start: "시작", merge: "취합", gate: "판정", end: "최종 합성" }[d.type] || d.type;
    },
    subtitle() {
      const d = this.data;
      if (d.type === "agent") return d.agent ? `${d.agent}${d.sees === "all" ? " · 전체 기록" : ""}` : "";
      if (d.type === "gate") return d.question || "질문을 적으세요";
      if (d.type === "start") return d.plan === false ? "요청만" : "요청 + 계획";
      if (d.type === "merge") return "orchestrator";
      return "기존 합성";
    },
    run() {
      return this.data.run || null;
    },
    runChip() {
      const r = this.run;
      if (!r) return "";
      if (r.state === "running") return "▶ 진행 중";
      if (r.state === "error") return "⚠ 실패";
      if (r.decision) return `${DECISION[r.decision] || r.decision}${r.visits > 1 ? " ×" + r.visits : ""}`;
      if (r.state === "done") return r.visits > 1 ? `✓ ×${r.visits}` : "✓";
      return "";
    },
    badges() {
      if (this.run) return this.run.capped ? ["상한"] : [];
      const out = [];
      if (this.data.max_visits) out.push(`최대 ${this.data.max_visits}회`);
      if (this.data.wait === "all") out.push("모두 기다림");
      return out;
    },
    bandStyle() {
      return this.data.type === "agent" && this.data.color ? { background: this.data.color } : null;
    },
  },
  template: `
    <div class="gnode" :class="['gnode-' + data.type, run ? 'gnode-run-' + run.state : '', { 'gnode-selected': selected, 'gnode-invalid': data.type === 'agent' && !data.agentName }]">
      <Handle v-if="ports.input" type="target" :position="Position.Left" id="in" class="gpin" />
      <div class="gnode-band" :style="bandStyle">
        <span>{{ kind }}</span>
        <span class="gnode-badges">{{ badges.join(' · ') }}</span>
      </div>
      <div v-if="runChip" class="gnode-chip" :class="run && run.decision ? 'gnode-chip-' + run.decision : ''">{{ runChip }}</div>
      <div class="gnode-title">{{ title }}</div>
      <div class="gnode-sub">{{ subtitle }}</div>
      <template v-if="data.type === 'gate'">
        <Handle type="source" :position="Position.Right" id="yes" class="gpin gpin-yes" style="top: 46%" />
        <span class="gport gport-yes">예</span>
        <Handle type="source" :position="Position.Right" id="no" class="gpin gpin-no" style="top: 80%" />
        <span class="gport gport-no">아니오</span>
      </template>
      <Handle v-else-if="ports.outputs.length" type="source" :position="Position.Right" id="out" class="gpin" />
    </div>`,
};

// 되돌아가는 선(판정 "아니오" → 앞 노드처럼 오른쪽에서 왼쪽으로). 기본 곡선은 노드 줄을 가로질러 노드 뒤에
// 숨고, 그 위의 "아니오 ✓" 표시도 함께 가려집니다. 줄 아래로 크게 돌아 들어가게 그립니다.
const LoopEdge = {
  props: ["id", "sourceX", "sourceY", "targetX", "targetY", "markerEnd", "label", "style", "data"],
  computed: {
    geo() {
      const sx = this.sourceX, sy = this.sourceY, tx = this.targetX, ty = this.targetY;
      // `loopY` 는 두 끝 사이에 놓인 노드들의 아랫변보다 아래 (routeEdges 가 잽니다).
      const low = Math.max(Math.max(sy, ty) + 90, (this.data && this.data.loopY) || 0);
      const mx = (sx + tx) / 2;
      return {
        d: `M${sx},${sy} C${sx + 140},${sy} ${sx + 140},${low} ${mx},${low} S${tx - 140},${ty} ${tx},${ty}`,
        lx: mx,
        ly: low,
        lw: Math.max(40, String(this.label || "").length * 8 + 16),
      };
    },
  },
  template: `
    <path class="vue-flow__edge-path" :d="geo.d" :marker-end="markerEnd" :style="style" fill="none" />
    <path class="vue-flow__edge-interaction" :d="geo.d" fill="none" stroke-opacity="0" stroke-width="20" />
    <g v-if="label" class="vue-flow__edge-textwrapper" :transform="'translate(' + geo.lx + ',' + geo.ly + ')'">
      <rect class="vue-flow__edge-textbg" :x="-geo.lw / 2" y="-9" :width="geo.lw" height="18" rx="4" />
      <text class="vue-flow__edge-text" text-anchor="middle" dominant-baseline="central">{{ label }}</text>
    </g>`,
};

const NODE_HEIGHT = 80; // 대략의 노드 높이 (좌표에는 크기가 없습니다)

function loopY(edge, nodes) {
  const source = nodes.find((n) => n.id === edge.source);
  const target = nodes.find((n) => n.id === edge.target);
  if (!source || !target) return 0;
  const left = Math.min(source.position.x, target.position.x) - 40;
  const right = Math.max(source.position.x, target.position.x) + 230;
  const between = nodes.filter((n) => n.position.x >= left && n.position.x <= right);
  return Math.max(...between.map((n) => n.position.y + NODE_HEIGHT)) + 36;
}

function isBackward(edge, nodes) {
  const source = nodes.find((n) => n.id === edge.source);
  const target = nodes.find((n) => n.id === edge.target);
  return !!(source && target && target.position.x <= source.position.x);
}

function edgeView(edge, run) {
  const carry = (edge.data && edge.data.carry) || "full";
  const branch = edge.sourceHandle === "yes" || edge.sourceHandle === "no" ? edge.sourceHandle : "";
  // 실행 표시: taken(흐름) · active(지금 도는 노드로 들어감) · idle(아직 안 흐름).
  const flow = run && run.edges ? run.edges[edge.id] || "idle" : "";
  let label = CARRY[carry] || carry;
  if (branch && flow && flow !== "idle") label = `${DECISION[branch]} ✓ · ${label}`;
  return {
    ...edge,
    data: { ...(edge.data || {}), carry },
    label,
    class: `gedge gedge-${carry}${branch ? " gedge-" + branch : ""}${flow ? " gedge-run-" + flow : ""}`,
    animated: flow === "active",
    markerEnd: MarkerType.ArrowClosed,
  };
}

export default {
  components: { VueFlow },
  // `runView` 는 처음 그릴 때의 실행 상태입니다. 만든 직후에는 `setRun` 을 부를 수 없어(아직 붙지 않음)
  // 속성으로 받고, 이후 변화는 `setRun` 으로 받습니다.
  props: { graph: Object, readonly: { type: Boolean, default: false }, runView: { type: Object, default: null } },
  data() {
    const run = this.runView && this.runView.has_run ? this.runView : null;
    return {
      nodes: (this.graph.nodes || []).map((n) => ({
        ...n, type: "mado", data: { ...n.data, run: run ? run.nodes[n.id] || null : null },
      })),
      edges: (this.graph.edges || []).map((e) => {
        const view = edgeView(e, run);
        const nodes = this.graph.nodes || [];
        const loop = isBackward(e, nodes);
        return { ...view, type: loop ? "loop" : "default", data: { ...view.data, loopY: loop ? loopY(e, nodes) : 0 } };
      }),
      nodeTypes: { mado: markRaw(GraphNode) },
      edgeTypes: { loop: markRaw(LoopEdge) },
      dirty: false,
      flow: null,
      // 사람이 화면을 옮기거나 확대하기 전까지는 캔버스 크기가 바뀔 때마다 다시 맞춥니다.
      viewTouched: false,
      run,
    };
  },
  template: `
    <div class="gcanvas">
      <VueFlow
        v-model:nodes="nodes"
        v-model:edges="edges"
        :node-types="nodeTypes"
        :edge-types="edgeTypes"
        :delete-key-code="readonly ? null : ['Delete', 'Backspace']"
        :nodes-draggable="!readonly"
        :nodes-connectable="!readonly"
        :elements-selectable="!readonly"
        :zoom-on-scroll="!readonly"
        :prevent-scrolling="!readonly"
        :is-valid-connection="isValidConnection"
        :min-zoom="0.2"
        :max-zoom="2"
        @pane-ready="onPaneReady"
        @nodes-initialized="refit"
        @move-start="(e) => { if (e && e.event) viewTouched = true; }"
        @connect="onConnect"
        @node-click="(e) => select('node', e.node.id)"
        @edge-click="(e) => select('edge', e.edge.id)"
        @pane-click="select(null, null)"
        @nodes-change="onChange"
        @edges-change="onChange"
      />
    </div>`,
  mounted() {
    this._beforeUnload = (e) => {
      if (this.dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", this._beforeUnload);
    // 첫 그리기 때 캔버스 크기가 0 이거나(숨은 탭·늦게 잡히는 레이아웃) 창 크기가 바뀌면, 맞춰 둔
    // 화면이 어긋나 노드가 밖으로 나갑니다. 사람이 직접 화면을 움직이기 전까지는 다시 맞춥니다.
    // Vue Flow 도 자기 크기를 ResizeObserver 로 재므로, 같은 순간에 맞추면 옛 크기로 계산합니다. 한 박자 늦춥니다.
    this._resize = new ResizeObserver(() => {
      if (this.viewTouched) return;
      clearTimeout(this._refit);
      this._refit = setTimeout(() => this.fit(), 80);
    });
    this._resize.observe(this.$el);
  },
  unmounted() {
    window.removeEventListener("beforeunload", this._beforeUnload);
    if (this._resize) this._resize.disconnect();
  },
  methods: {
    onPaneReady(instance) {
      this.flow = markRaw(instance);
      // 첫 맞춤은 캔버스 크기가 잡힌 뒤에 합니다. 바로 하면 0 크기로 계산돼 아주 작게 보입니다.
      // 대화상자처럼 늦게 자리 잡는 곳을 위해 한 번 더 합니다.
      setTimeout(() => this.fit(), 60);
      setTimeout(() => this.refit(), 400);
    },
    refit() {
      if (!this.viewTouched) setTimeout(() => this.fit(), 30);
    },
    fit() {
      if (!this.flow) return;
      // 숨어 있거나 자리 잡는 중(대화상자 전환 효과)에 그려진 노드는 핀 자리가 0 으로 재어져, 선이 노드
      // 윗변에 겹쳐 그려집니다. 맞추기 전에 다시 재고, 잰 값이 반영된 뒤에 맞춥니다.
      this.flow.updateNodeInternals();
      setTimeout(() => {
        // 화면 맞춤 버튼은 사람이 화면을 옮긴 뒤에도 맞춰야 하므로 여기서는 viewTouched 를 보지 않습니다.
        if (this.flow) this.flow.fitView({ padding: 0.2, maxZoom: 1 });
      }, 30);
    },
    markDirty() {
      if (!this.dirty) {
        this.dirty = true;
        this.$emit("dirty");
      }
    },
    markClean() {
      this.dirty = false;
    },
    onChange(changes) {
      // 선택·크기 측정 같은 변화는 내용이 바뀐 것이 아닙니다.
      if (changes.some((c) => c.type === "remove" || c.type === "add" || (c.type === "position" && c.dragging === false))) {
        this.markDirty();
        this.routeEdges();
      }
      if (changes.some((c) => c.type === "remove")) this.select(null, null);
    },
    isValidConnection(conn) {
      // Vue Flow 는 끌어 이을 때뿐 아니라 선 목록을 새로 받을 때마다 이미 있는 선도 이 함수로 다시
      // 검사해 틀린 것을 버립니다. 그래서 중복 검사에서 자기 자신(같은 id)은 빼야 합니다.
      if (conn.source === conn.target) return false;
      const target = this.nodes.find((n) => n.id === conn.target);
      if (!target || !(PORTS[target.data.type] || {}).input) return false;
      return !this.edges.some(
        (e) => e.id !== conn.id && e.source === conn.source && e.sourceHandle === conn.sourceHandle && e.target === conn.target,
      );
    },
    onConnect(conn) {
      if (!this.isValidConnection(conn)) return;
      const id = this.freeId("e", this.edges);
      this.edges = [...this.edges, edgeView({ id, ...conn, data: { carry: "full" } })];
      this.routeEdges();
      this.markDirty();
      this.select("edge", id);
    },
    routeEdges() {
      // 노드를 옮기면 앞뒤가 바뀔 수 있어 그때마다 다시 고릅니다 (제자리에서, 새 객체 없이).
      for (const edge of this.edges) {
        const loop = isBackward(edge, this.nodes);
        const type = loop ? "loop" : "default";
        if (edge.type !== type) edge.type = type;
        const y = loop ? loopY(edge, this.nodes) : 0;
        if ((edge.data || {}).loopY !== y) edge.data = { ...edge.data, loopY: y };
      }
    },
    freeId(prefix, items) {
      const taken = new Set(items.map((i) => i.id));
      let n = 1;
      while (taken.has(prefix + n)) n += 1;
      return prefix + n;
    },
    select(kind, id) {
      if (this.readonly) return;
      let payload = { kind: null, id: null, data: null };
      if (kind === "node") {
        const node = this.nodes.find((n) => n.id === id);
        if (node) payload = { kind, id, data: { ...node.data } };
      } else if (kind === "edge") {
        const edge = this.edges.find((e) => e.id === id);
        if (edge) {
          payload = {
            kind, id,
            data: { carry: edge.data.carry, from: [edge.source, edge.sourceHandle], to: [edge.target, edge.targetHandle] },
          };
        }
      }
      this.$emit("select", payload);
    },
    center() {
      if (!this.flow) return { x: 80, y: 80 };
      const rect = this.$el.getBoundingClientRect();
      const { x, y, zoom } = this.flow.getViewport();
      return { x: (rect.width / 2 - x) / zoom - 90, y: (rect.height / 2 - y) / zoom - 45 };
    },
    addNode(data) {
      // 시작·끝은 처음 하나를 `start` · `end` 로 둡니다 (카드 순서로 만든 그래프와 같은 이름).
      const single = (data.type === "start" || data.type === "end") && !this.nodes.some((n) => n.id === data.type);
      const id = single ? data.type : this.freeId(data.type === "agent" ? "n" : data.type, this.nodes);
      const base = this.center();
      const offset = (this.nodes.length % 5) * 24;
      this.nodes = [...this.nodes, { id, type: "mado", position: { x: base.x + offset, y: base.y + offset }, data: { ...data } }];
      this.markDirty();
      this.$nextTick(() => this.select("node", id));
      return id;
    },
    // 노드·선은 **제자리에서** 고칩니다. 새 객체로 바꿔 끼우면 Vue Flow 가 같은 id 의 노드를 다시 재지 않아
    // 크기가 0 이 되고, 화면 맞춤(fitView)이 아무것도 하지 않습니다. 이 배열의 객체가 Vue Flow 내부 상태와
    // 같은 객체라 속성을 바꾸면 그대로 반영됩니다.
    updateNode(id, patch) {
      const node = this.nodes.find((n) => n.id === id);
      if (node) node.data = { ...node.data, ...patch };
      this.markDirty();
    },
    updateEdge(id, patch) {
      const edge = this.edges.find((e) => e.id === id);
      if (edge) this.restyleEdge(edge, { ...edge.data, ...patch }, this.run && this.run.has_run ? this.run : null);
      this.markDirty();
    },
    restyleEdge(edge, data, run) {
      const view = edgeView({ ...edge, data }, run);
      edge.data = view.data;
      edge.label = view.label;
      edge.class = view.class;
      edge.animated = view.animated;
    },
    removeElement(kind, id) {
      if (kind === "node") {
        this.nodes = this.nodes.filter((n) => n.id !== id);
        this.edges = this.edges.filter((e) => e.source !== id && e.target !== id);
      } else if (kind === "edge") {
        this.edges = this.edges.filter((e) => e.id !== id);
      }
      this.markDirty();
      this.select(null, null);
    },
    setRun(view) {
      // 실행 상태만 바꿉니다. 노드 자리와 화면 위치는 그대로 둡니다.
      this.run = view || null;
      const run = this.run && this.run.has_run ? this.run : null;
      for (const node of this.nodes) node.data = { ...node.data, run: run ? run.nodes[node.id] || null : null };
      for (const edge of this.edges) this.restyleEdge(edge, edge.data, run);
    },
    replaceGraph(graph) {
      // 같은 id(start·end·n1…)가 다시 오므로, 한 박자 비워 두었다가 채워 노드를 새로 재게 합니다.
      this.nodes = [];
      this.edges = [];
      this.$nextTick(() => {
        this.nodes = (graph.nodes || []).map((n) => ({ ...n, type: "mado" }));
        this.edges = (graph.edges || []).map((e) => edgeView(e));
        this.routeEdges();
        this.markDirty();
        this.select(null, null);
        setTimeout(() => this.fit(), 60);
      });
    },
    getGraph() {
      return {
        nodes: this.nodes.map((n) => {
          const out = { id: n.id, pos: [Math.round(n.position.x), Math.round(n.position.y)] };
          for (const f of FIELDS) if (n.data[f] !== undefined && n.data[f] !== null && n.data[f] !== "") out[f] = n.data[f];
          return out;
        }),
        edges: this.edges.map((e) => ({
          id: e.id,
          from: [e.source, e.sourceHandle || "out"],
          to: [e.target, e.targetHandle || "in"],
          carry: (e.data && e.data.carry) || "full",
        })),
      };
    },
  },
};

# 그래프 편집기 라이브러리 묶음

`index.js` 는 [Vue Flow](https://vueflow.dev) 를 NiceGUI 의 `esm=` 로 붙이기 위해 하나의 ES 모듈로 묶은 것입니다.
폐쇄망에서 CDN 없이 뜨도록 저장소에 싣습니다. `vue` 는 묶지 않습니다 — NiceGUI 가 importmap 에 올린 Vue 를
함께 써야 컴포넌트가 같은 Vue 인스턴스에서 돕니다.

| 파일 | 내용 |
| --- | --- |
| `index.js` | `@vue-flow/core` 1.48.2 + 의존성 (vue 제외), 압축 |
| `vue-flow.css` | `@vue-flow/core/dist/style.css` + `theme-default.css` |
| `THIRD_PARTY_NOTICES.txt` | 묶인 패키지의 라이선스 원문 (MIT · ISC · BSD-3-Clause) |

다시 만들기 (인터넷이 되는 PC에서, 저장소 밖 임시 폴더):

```bash
npm init -y
npm install @vue-flow/core@1.48.2 esbuild@0.28.2 --legacy-peer-deps --no-audit --no-fund
echo 'export { VueFlow, Handle, Position, MarkerType, useVueFlow, ConnectionMode } from "@vue-flow/core";' > entry.js
npx esbuild entry.js --bundle --format=esm --external:vue --minify --target=es2020   --define:process.env.NODE_ENV='"production"' --legal-comments=none --outfile=index.js
cat node_modules/@vue-flow/core/dist/style.css node_modules/@vue-flow/core/dist/theme-default.css > vue-flow.css
```

`--legacy-peer-deps` 는 peer 의존성인 `vue` 를 받지 않게 합니다. 폴더 이름을 `vendor` 로 하지 마세요 —
`package_source.py` 가 런타임 혼입을 막으려고 그 이름을 거부합니다.

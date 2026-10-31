// Render the production observation component against a live isolated plugin host.
import { pathToFileURL } from "node:url";
import path from "node:path";

const [dashboardRoot, backend] = process.argv.slice(2);
const { createServer } = await import(pathToFileURL(path.join(dashboardRoot, "node_modules/vite/dist/node/index.js")));
const { default: vue } = await import(pathToFileURL(path.join(dashboardRoot, "node_modules/@vitejs/plugin-vue/dist/index.mjs")));
const html = `<!doctype html><meta charset="utf-8"><title>Agent browser validation</title>
<style>body{margin:0;background:#eef1f5;font-family:Arial}#app{width:900px;height:680px;margin:24px auto;background:white;border:1px solid #ccc}</style>
<div id="app"></div><script type="module">
import {createApp, h, ref, computed} from 'vue';
import {createPinia} from 'pinia';
import {createVuetify} from 'vuetify';
import * as components from 'vuetify/components';
import 'vuetify/styles';
import '@mdi/font/css/materialdesignicons.css';
import GitDiffSidebar from '/src/components/chat/GitDiffSidebar.vue';
import GitDiffChip from '/src/components/chat/GitDiffChip.vue';
import {provideSpcodeSession} from '/src/composables/useSpcodeSession.ts';
import {useSpcodeProjectStatus} from '/src/composables/useSpcodeProjectStatus.ts';
import {initI18n} from '/src/i18n/composables.ts';
import {setupHttpClient} from '/src/api/http.ts';
localStorage.setItem('token','observer-probe-token');
setupHttpClient(); await initI18n(new URLSearchParams(location.search).get('locale') || 'zh-CN');
const visible = ref(true);
const open = ref(false);
const chatId = ref('chat-1');
createApp({setup() {
  const status = useSpcodeProjectStatus();
  const project = (loaded) => { status.status.value = {loaded, directory:loaded?'C:/observer-fixture':null, umo:'webchat:FriendMessage:webchat!alice!'+chatId.value, loadedAt:1, allLoadedCount:loaded?1:0, fetchedAt:Date.now(), bootId:null}; };
  project(false);
  provideSpcodeSession({umo:computed(()=>status.status.value.umo),directory:computed(()=>status.status.value.directory)});
  window.observerProbe = {
    setVisible(value) {visible.value = value;},
    setProject:project,
    setChat(value) {chatId.value=value;project(false);},
  };
  return ()=>h('div',{class:'v-theme--light',style:'display:flex;flex-direction:column;height:100%'},[
    h('div',{style:'padding:8px'},[h(GitDiffChip,{browserOnly:!status.status.value.loaded,onToggleDiffSidebar:()=>open.value=!open.value})]),
    h('div',{style:'display:flex;flex:1;min-height:0'},[h(GitDiffSidebar,{chatId:chatId.value,modelValue:visible.value&&open.value,'onUpdate:modelValue':value=>open.value=value})]),
  ]);
}}).use(createPinia()).use(createVuetify({components})).mount('#app');
</script>`;
const server = await createServer({
  configFile: false, root: dashboardRoot,
  plugins: [vue(), {name:"observer-probe", configureServer(server) {
    server.middlewares.use('/observer-probe', async (_request, response) => {
      response.setHeader('Content-Type', 'text/html');
      response.end(await server.transformIndexHtml('/observer-probe', html));
    });
  }}],
  resolve: {alias: {'@':path.join(dashboardRoot, 'src')}},
  optimizeDeps: {include:['vue'], entries:[]},
  server: {host:'127.0.0.1',port:0,proxy:{'/api':backend}},
});
await server.listen();
console.log(`PROBE_URL=http://127.0.0.1:${server.httpServer.address().port}/observer-probe`);

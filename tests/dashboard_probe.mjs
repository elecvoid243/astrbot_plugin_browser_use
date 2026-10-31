// Render the production observation component against a live isolated plugin host.
import { pathToFileURL } from "node:url";
import path from "node:path";

const [dashboardRoot, backend] = process.argv.slice(2);
const { createServer } = await import(pathToFileURL(path.join(dashboardRoot, "node_modules/vite/dist/node/index.js")));
const { default: vue } = await import(pathToFileURL(path.join(dashboardRoot, "node_modules/@vitejs/plugin-vue/dist/index.mjs")));
const html = `<!doctype html><meta charset="utf-8"><title>Agent browser validation</title>
<style>body{margin:0;background:#eef1f5;font-family:Arial}#app{width:900px;height:680px;margin:24px auto;background:white;border:1px solid #ccc}</style>
<div id="app"></div><script type="module">
import {createApp, h} from 'vue';
import {createVuetify} from 'vuetify';
import {VBtn} from 'vuetify/components';
import 'vuetify/styles';
import AgentBrowserView from '/src/components/chat/AgentBrowserView.vue';
import {initI18n} from '/src/i18n/composables.ts';
import {setupHttpClient} from '/src/api/http.ts';
localStorage.setItem('token','observer-probe-token');
setupHttpClient(); await initI18n(new URLSearchParams(location.search).get('locale') || 'zh-CN');
createApp({render:()=>h('div',{class:'v-theme--light',style:'display:flex;height:100%'},[h(AgentBrowserView,{chatId:'chat-1',visible:true})])}).use(createVuetify({components:{VBtn}})).mount('#app');
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

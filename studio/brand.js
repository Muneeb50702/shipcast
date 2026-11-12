// Shipcast code-level rebrand of the built frontend (run by rebrand.sh inside the image build, cwd = apps/frontend).
// 1. Colours: upstream purples -> Shipcast oranges, in every form the build uses (hex any case, %23-encoded, rgb()).
// 2. Settings: the upstream "CLI & AI Skills" + "MCP Client Configuration" cards (upstream CLI, docs, marketplace
//    links) become one card on connecting Claude to Shipcast's own MCP link (SHIPCAST_CONTROL/mcp, OAuth sign-in).
// 3. Agent page (upstream chat that needs an OpenAI key) shows Shipcast Control (/control/, same site and login) in
//    an iframe; its menu item becomes "Claude".
// 4. Every link to the upstream product's own sites is neutralised and hidden (see UPSTREAM below + brand.css).
// 5. Backend: LinkedIn profiles and company pages use separate LinkedIn apps (see the end of this file).
// Every patch must hit, or the build fails (so an upstream update can't silently drop it).
const fs = require("fs"), path = require("path");

const CONTROL = (process.env.SHIPCAST_CONTROL || "").replace(/\/$/, "");
if (!/^https:\/\/[\w.-]+(\/[\w-]+)*$/.test(CONTROL)) throw new Error("SHIPCAST_CONTROL must look like https://studio.example.com/control");
const MCP = CONTROL + "/mcp";
const CMD = `claude mcp add --transport http --scope user shipcast ${MCP}`;

const walk = d => fs.readdirSync(d, { withFileTypes: true }).flatMap(e => {
  const p = path.join(d, e.name);
  if (e.name === "cache") return [];
  if (e.isDirectory()) return walk(p);
  return e.isFile() ? [p] : [];  // skip symlinks (some point at directories)
});
const files = walk(".next").filter(f => {
  const b = fs.readFileSync(f);
  return !b.subarray(0, 8000).includes(0);  // text files only, like grep -I
});

// ---- 1. colours: old -> new (hex); the rgb() triplets are derived
const COLOURS = {
  "612BD3": "CF4410", "612AD5": "CF4410", "8155DD": "E5622A", "FC69FF": "FF8A4C", "5520CB": "A8360B",
  "5023B8": "A8360B", "7B3FF2": "E8551F", "622AFF": "E8400C", "7C3AFF": "FF5A1F", "8B5CF6": "FF7A2F",
  "9D6EFF": "FF8A4C", "B69DEC": "FFB089", "C4B5FD": "FFC4A3", "A78BFA": "FF9A66",
};
const rgb = h => [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16)).join(" ");
const RGB = Object.fromEntries(Object.entries(COLOURS).map(([o, n]) => [rgb(o), rgb(n).split(" ")]));
const HEX_RE = new RegExp(`(#|%23)(${Object.keys(COLOURS).join("|")})`, "gi");
const RGB_RE = /(rgba?\()(\d{1,3})([ ,]+)(\d{1,3})([ ,]+)(\d{1,3})/g;
const recolour = s => s
  .replace(HEX_RE, (m, p, h) => p + (h === h.toLowerCase() ? COLOURS[h.toUpperCase()].toLowerCase() : COLOURS[h.toUpperCase()]))
  .replace(RGB_RE, (m, p, r, s1, g, s2, b) => {
    const n = RGB[`${r} ${g} ${b}`];
    return n ? `${p}${n[0]}${s1}${n[1]}${s2}${n[2]}` : m;
  });

// ---- 2/3. the guide card, as compiled JSX for the module's jsx runtime alias T. Classes are reused from the
// upstream cards (Tailwind only ships classes the build already uses).
const J = JSON.stringify;
const C = {
  card: "bg-newBgColorInnerInner rounded-[12px] border border-newBorder overflow-hidden",
  head: "bg-newBgColorInner px-[20px] mobile:px-[14px] py-[14px] border-b border-newBorder flex items-start justify-between gap-[12px] mobile:flex-col",
  title: "text-[15px] font-[600]",
  sub: "text-[13px] text-customColor18 mt-[2px]",
  body: "p-[20px] mobile:p-[14px] flex flex-col gap-[16px]",
  col: "flex flex-col gap-[6px]",
  label: "text-[13px] font-[600] text-customColor18",
  code: "bg-newBgColorInner border border-newBorder rounded-[8px] p-[16px] text-[13px] whitespace-pre-wrap break-all overflow-x-auto leading-[1.6]",
  text: "text-[14px] text-textColor leading-[1.7]",
  row: "flex flex-wrap gap-[8px]",
  btn: "cursor-pointer px-[16px] h-[36px] bg-btnSimple hover:bg-boxHover transition-colors rounded-[8px] text-[13px] font-[600] flex items-center gap-[6px]",
  primary: "cursor-pointer px-[16px] h-[36px] bg-[#CF4410] hover:bg-[#A8360B] text-white transition-colors rounded-[8px] text-[13px] font-[600] flex items-center gap-[6px]",
};
const guide = T => {
  const h = (tag, cls, kids, extra = "") => `(0,${T}.${kids.length > 1 ? "jsxs" : "jsx"})(${J(tag)},{` +
    `${cls ? `className:${J(cls)},` : ""}${extra}children:${kids.length > 1 ? `[${kids.join(",")}]` : kids[0]}})`;
  const copy = (text, label) => h("button", C.btn, [J(label)], `type:"button",onClick:e=>{navigator.clipboard.` +
    `writeText(${J(text)});e.currentTarget.textContent="Copied"},`);
  const step = (title, ...kids) => h("div", C.col, [h("div", C.label, [J(title)]), ...kids]);
  const text = s => h("div", C.text, [J(s)]);
  return h("div", C.card, [
    h("div", C.head, [
      h("div", "", [h("div", C.title, [J("Connect Claude to Shipcast (MCP)")]),
        h("div", C.sub, [J("One link gives Claude every Shipcast tool for your workspace. You approve each app once.")])]),
      h("a", C.primary, [J("Open Claude page")], `href:"/agents",`)]),
    h("div", C.body, [
      step("1. Your MCP link", h("pre", C.code, [J(MCP)]), h("div", C.row, [copy(MCP, "Copy link")])),
      step("2a. Claude Code", h("pre", C.code, [J(CMD)]), text("Then type /mcp, pick shipcast and choose Authenticate."),
        h("div", C.row, [copy(CMD, "Copy command")])),
      step("2b. claude.ai, Claude Desktop or any other MCP app",
        text("Settings → Connectors → Add custom connector, then paste the MCP link.")),
      step("3. Approve", text("A Shipcast page opens. While logged in here, click Allow. " +
        "You can see and disconnect your apps on the Claude page.")),
    ])]);
};

const PATCHES = [
  { name: "settings cards", where: "static", // <CliCard apiKey/> <McpCard user mcpBase/> -> guide
    re: /\(0,([\w$]+)\.jsx\)\(([\w$]+),\{apiKey:([\w$]+)\.publicApi\}\),\(0,\1\.jsx\)\(([\w$]+),\{user:\3,mcpBase:[^}]+\}\)/g,
    to: (m, T) => guide(T) },
  { name: "agent page", where: "static", // Agent layout renders Control instead of the chat (hooks never run)
    re: /\["Agent",0,\(\{children:([\w$]+)\}\)=>\{(let\[[\w$]+,[\w$]+\]=\(0,[\w$]+\.useState\)\(\[\]\);return\(0,([\w$]+)\.jsxs\))/g,
    to: (m, kids, rest, T) => `["Agent",0,({children:${kids}})=>{return ` +
      `(0,${T}.jsx)("iframe",{src:"/control/",title:"Shipcast Claude",style:{width:"100%",flex:1,border:0,` +
      `display:"block",minHeight:"calc(100vh - 120px)"}});${rest}` },
  { name: "agent menu", where: "static", re: /\{name:"Agent",icon:/g, to: () => `{name:"Claude",icon:` },
  { name: "agent title", where: "server", re: /Shipcast - Agent\b/g, to: () => "Shipcast - Claude" },
  { name: "extension button", where: "static", // "install the (upstream) browser extension" button: dropped, Cancel stays
    re: /\(0,[\w$]+\.jsx\)\([\w$.]+,\{type:"button",className:"flex-1",onClick:\(\)=>\{window\.open\("https:\/\/chromewebstore\.google\.com[^"]*","_blank"\),[\w$]+\.closeCurrent\(\)\},children:[\w$]+\("install_extension","Install Extension"\)\}\),/g,
    to: () => "" },
  // onboarding wizard (after sign-up): step 2 shows the guide; step 3 (upstream's tutorial video) is dropped and
  // step 2's Continue finishes the wizard
  { name: "wizard steps", where: "static",
    re: /\[([\w$]+)\("connect_channels","Connect Channels"\),\1\("connect_agents","Connect Agents"\),\1\("watch_tutorial","Watch Tutorial"\)\]/g,
    to: (m, a) => `[${a}("connect_channels","Connect Channels"),"Connect Claude"]` },
  { name: "wizard finish", where: "static",
    re: /(2===([\w$]+)&&\(0,([\w$]+)\.jsx\)\(([\w$]+),\{onBack:\(\)=>([\w$]+)\(1\),onNext:)\(\)=>\5\(3\)\}\),3===\2&&\(0,\3\.jsx\)\(([\w$]+),\{onBack:\(\)=>\5\(2\),onFinish:([\w$]+)\}\)/g,
    to: (m, head, i, T, H, r, R, finish) => `${head}${finish}})` },
  { name: "wizard agent step", where: "static", // agent tabs + upstream connector/MCP/CLI cards -> guide
    re: /([\w$]+)\?\(0,([\w$]+)\.jsxs\)\("div",\{className:"flex flex-col gap-\[16px\] w-full max-w-\[1100px\] mx-auto",children:\[[\s\S]*?"agent_access_unavailable","[^"]*"\)\}\)/g,
    to: (m, c, T) => `(0,${T}.jsx)("div",{className:"flex flex-col gap-[16px] w-full max-w-[1100px] mx-auto",` +
      `children:${guide(T)}})` },
];

// English UI texts that pointed at the removed agent options (component defaults and the English locale)
const TEXTS = {
  "Connect Your AI Agent": "Connect Claude",
  "Pick the agent you use and let it create and schedule posts for you":
    "Add Shipcast to Claude with one link, then let it create and schedule posts for you",
  "More agents and full instructions are available under Settings > Agents / Dev":
    "You can find these steps again under Claude in the sidebar",
};
const textHits = Object.fromEntries(Object.keys(TEXTS).map(k => [k, 0]));

// ---- 4. links to the upstream product's own sites (docs, terms, affiliate, repo, marketplaces, extension, n8n
// node) become "#shipcast-hidden"; brand.css hides every link carrying it. Runs after PATCHES.
const UPSTREAM = /https?:\/\/(?:(?:[a-z0-9-]+\.)*(?:postiz\.com|gitroom\.com)|www\.youtube\.com\/embed\/BdsCVvEYgHU|github\.com\/gitroomhq|chromewebstore\.google\.com\/detail\/postiz|claude\.ai\/directory\/postiz|cursor\.com\/marketplace\/postiz|x\.ai\/bot\/plugin|chatgpt\.com\/plugins|(?:www\.)?npmjs\.com\/package\/n8n-nodes-postiz|(?:www\.)?linkedin\.com\/company\/gitroom)(?:[\/?#][^"'`\\\s)]*)?/g;
let unlinked = 0;
const unlink = s => s
  .replaceAll("https://uploads.gitroom.com/F6LSCD8wrrQ.jpeg", "/shipcast-512.png")  // sample-post avatar
  .replaceAll("https://www.linkedin.com/company/gitroom", "https://www.linkedin.com/company/your-company")  // placeholder
  .replaceAll("The Shipcast browser extension is not installed. You need to install it before connecting this channel.",
              "This channel needs a browser extension, which Shipcast doesn't offer yet.")
  .replace(UPSTREAM, () => { unlinked++; return "#shipcast-hidden"; })
  .replace(/window\.open\("#shipcast-hidden"(?:,"_blank")?\)/g, "void 0");

const hits = Object.fromEntries(PATCHES.map(p => [p.name, { static: 0, server: 0 }]));
let recoloured = 0;
for (const f of files) {
  const s = fs.readFileSync(f, "utf8");
  let out = recolour(s);
  if (out !== s) recoloured++;
  if (f.endsWith(".js")) {
    const area = f.startsWith(".next/static/") ? "static" : "server";
    for (const p of PATCHES) {
      out = out.replace(p.re, (...a) => { hits[p.name][area]++; return p.to(...a); });
    }
  }
  if (!f.endsWith(".map")) {
    out = unlink(out);
    for (const [from, to] of Object.entries(TEXTS)) {
      if (out.includes(from) && f.startsWith(".next/static/")) textHits[from]++;
      out = out.replaceAll(from, to);
    }
  }
  if (out !== s) fs.writeFileSync(f, out);
}
console.log(`colours: ${recoloured} files; upstream links hidden: ${unlinked}; patches:`, JSON.stringify(hits));
for (const p of PATCHES) {
  if (!hits[p.name][p.where]) throw new Error(`patch "${p.name}" found nothing in ${p.where} files`);
}
for (const [t, n] of Object.entries(textHits)) if (!n) throw new Error(`text not found in static files: ${t}`);

// ---- 5. backend: LinkedIn split. LinkedIn now requires the Community Management API (company pages) to be the only
// product on its app, so one app can no longer grant everything upstream asks for. Personal profiles use the
// OpenID + Share on LinkedIn app (LINKEDIN_CLIENT_ID); pages use a separate Community Management app
// (LINKEDIN_PAGE_CLIENT_ID, falling back to LINKEDIN_CLIENT_ID), which has no openid/profile, so /v2/me replaces
// /v2/userinfo there. Patched in both the API server and the orchestrator (publishing worker).
const LI_ALL = /this\.scopes = \[\s*'openid',\s*'profile',\s*'w_member_social',\s*'r_basicprofile',\s*'rw_organization_admin',\s*'w_organization_social',\s*'r_organization_social',?\s*\]/g;
const BACKEND_PATCHES = [
  { file: "linkedin.provider.js", re: LI_ALL, n: 1, to: "this.scopes = ['openid', 'profile', 'w_member_social']" },
  { file: "linkedin.page.provider.js", re: LI_ALL, n: 1,
    to: "this.scopes = ['r_basicprofile', 'w_member_social', 'rw_organization_admin', 'w_organization_social', 'r_organization_social']" },
  { file: "linkedin.page.provider.js", re: /process\.env\.LINKEDIN_CLIENT_ID/g, n: 3,
    to: "(process.env.LINKEDIN_PAGE_CLIENT_ID || process.env.LINKEDIN_CLIENT_ID)" },
  { file: "linkedin.page.provider.js", re: /process\.env\.LINKEDIN_CLIENT_SECRET/g, n: 2,
    to: "(process.env.LINKEDIN_PAGE_CLIENT_SECRET || process.env.LINKEDIN_CLIENT_SECRET)" },
  { file: "linkedin.page.provider.js", n: 2,
    re: /const \{\s*name,\s*sub: id,\s*picture,\s*\} = await \(await fetch\('https:\/\/api\.linkedin\.com\/v2\/userinfo'/g,
    to: "const { localizedFirstName: name, id, picture, } = await (await fetch('https://api.linkedin.com/v2/me'" },
];
for (const app of ["backend", "orchestrator"]) {
  const dir = `/app/apps/${app}/dist/libraries/nestjs-libraries/src/integrations/social/`;
  for (const p of BACKEND_PATCHES) {
    let hits = 0;
    const out = fs.readFileSync(dir + p.file, "utf8").replace(p.re, () => { hits++; return p.to; });
    if (hits !== p.n) throw new Error(`${app}/${p.file}: expected ${p.n} match(es) of ${p.re}, got ${hits}`);
    fs.writeFileSync(dir + p.file, out);
  }
}
console.log("backend: linkedin split patched in backend + orchestrator");
const anyUpstream = new RegExp(UPSTREAM.source);  // no /g: .test() must not carry lastIndex between files
const linked = files.filter(f => !f.endsWith(".map") && anyUpstream.test(fs.readFileSync(f, "utf8")));
if (!unlinked || linked.length) throw new Error("upstream links left in " + linked.join(", "));
const left = files.filter(f => /\.(js|css)$/.test(f) && /#(612bd3|5520cb|fc69ff)|rgb\(97 43 211|rgb\(85 32 203/i.test(fs.readFileSync(f, "utf8")));
if (left.length) throw new Error("old colours left in " + left.join(", "));

/* =====================================================================
   Painel do Karuta — logica.

   Os graficos sao SVG montado na mao: o painel nao pode puxar biblioteca
   de fora (a pagina eh servida pelo proprio bot, offline).
   ===================================================================== */

const SECOES = [
  { id: "geral", rotulo: "visao geral" },
  { id: "cartas", rotulo: "cartas", tabela: "cartas", metrica: "esforco_total", metricaRot: "esforco" },
  { id: "job", rotulo: "job", tabela: "cartas_job", metrica: "esforco_total", metricaRot: "esforco" },
  { id: "desejadas", rotulo: "desejadas", tabela: "cartas_desejadas", metrica: "desejos", metricaRot: "desejos" },
];
const CONTAS = ["a", "b", "c"];

const COLUNAS = {
  cartas: [
    ["conta_id", "conta", "conta"], ["codigo", "codigo", "codigo"],
    ["personagem", "personagem", "nome"], ["serie", "serie", "serie"],
    ["classe", "classe", "classe"],
    ["esforco_total", "esforco", "barra"], ["valor_base", "base", "num"],
    ["desejos", "desejos", "num"], ["circulacao", "circulacao", "num"],
    ["taxa_reivindicacao", "claim %", "num"],
    ["estrelas", "estrelas", "estrelas"], ["numero_print", "print", "print"],
    ["edicao", "ed", "num"], ["condicao", "condicao", "txt"],
    ["ouro_burn", "ouro", "num"], ["poeira_burn", "poeira", "num"],
    ["origem", "origem", "txt"],
    ["obtida_em", "obtida", "data"], ["avaliada_em", "avaliada", "data"],
  ],
  job: [
    ["conta_id", "conta", "conta"], ["codigo", "codigo", "codigo"],
    ["personagem", "personagem", "nome"], ["serie", "serie", "serie"],
    ["esforco_total", "esforco", "barra"], ["valor_base", "base", "num"],
    ["desejos", "desejos", "num"],
    ["estrelas", "estrelas", "estrelas"], ["numero_print", "print", "print"],
    ["edicao", "ed", "num"], ["condicao", "condicao", "txt"],
    ["motivo", "motivo", "txt"], ["classificada_em", "classificada", "data"],
  ],
  desejadas: [
    ["conta_id", "conta", "conta"], ["codigo", "codigo", "codigo"],
    ["personagem", "personagem", "nome"], ["serie", "serie", "serie"],
    ["desejos", "desejos", "barra"], ["circulacao", "circulacao", "num"],
    ["taxa_reivindicacao", "claim %", "num"],
    ["esforco_total", "esforco", "num"], ["valor_base", "base", "num"],
    ["estrelas", "estrelas", "estrelas"], ["numero_print", "print", "print"],
    ["edicao", "ed", "num"], ["condicao", "condicao", "txt"],
    ["motivo", "motivo", "txt"], ["classificada_em", "classificada", "data"],
  ],
};
const TIPO_NUM = new Set(["num", "barra", "print", "estrelas"]);

// Filtros rapidos por secao. Sem isto a secao `cartas` abre numa parede de
// "—": as mais recentes sao do scan e ainda nao passaram pelo k!wi.
//
// Em `cartas` o filtro roda no SERVIDOR (`servidor: true`): a tabela tem
// milhares de linhas e o painel carrega so umas centenas, entao filtrar no
// cliente devolvia zero resultado. Nas outras secoes a tabela inteira cabe
// na pagina e o filtro eh local, sem ida ao banco.
const CHIPS = {
  cartas: [
    { id: "todas", rot: "todas", servidor: true },
    { id: "avaliadas", rot: "avaliadas", servidor: true },
    { id: "pendentes", rot: "pendentes", servidor: true },
    { id: "job", rot: "job", servidor: true },
    { id: "desejada", rot: "desejada", servidor: true },
    { id: "estrelas", rot: "3★+", servidor: true },
  ],
  job: [
    { id: "todas", rot: "todas", f: () => true },
    { id: "estrelas", rot: "3★+", f: (l) => (l.estrelas || 0) >= 3 },
    { id: "alto", rot: "esforco 50+", f: (l) => (l.esforco_total || 0) >= 50 },
    { id: "pedida", rot: "desejos 50+", f: (l) => (l.desejos || 0) >= 50 },
  ],
  desejadas: [
    { id: "todas", rot: "todas", f: () => true },
    { id: "estrelas", rot: "3★+", f: (l) => (l.estrelas || 0) >= 3 },
    { id: "alto", rot: "desejos 100+", f: (l) => (l.desejos || 0) >= 100 },
    { id: "boa", rot: "esforco 30+", f: (l) => (l.esforco_total || 0) >= 30 },
  ],
};

let dados = null, secao = "geral", conta = "", visao = "tabela", chip = "todas";
let ordem = null, timer = null, ultimo = null, carregando = false, redesenho = null;

/* ======================================================================
   Utilidades
   ====================================================================== */
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const n = (v) => (v === null || v === undefined ? null : Number(v));
const fmt = (v) => (v === null || v === undefined ? "—" : Number(v).toLocaleString("pt-BR"));
const compacto = (v) => {
  if (v === null || v === undefined) return "—";
  const x = Number(v);
  if (x >= 1e6) return (x / 1e6).toLocaleString("pt-BR", { maximumFractionDigits: 1 }) + " mi";
  if (x >= 10000) return (x / 1000).toLocaleString("pt-BR", { maximumFractionDigits: 1 }) + " mil";
  return x.toLocaleString("pt-BR");
};
const fmtData = (v) => {
  if (!v) return "—";
  return new Date(v).toLocaleString("pt-BR",
    { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
};
const corta = (s, k) => (s && s.length > k ? s.slice(0, k - 1) + "…" : s || "");
const secaoAtual = () => SECOES.find((s) => s.id === secao);
const ehTabela = () => secao !== "geral";
const linhas = () => (dados && ehTabela() && dados[secao]) || [];
const resumoSel = () => (dados ? dados.resumo.filter((r) => !conta || r.conta_id === conta) : []);
const soma = (campo) => resumoSel().reduce((t, r) => t + (Number(r[campo]) || 0), 0);
const maxDe = (campo) => linhas().reduce((m, l) => Math.max(m, Number(l[campo]) || 0), 0);
const corConta = (c) => `var(--conta-${c})`;

// Sem dado nao desenha trilho nenhum: trilho vazio se le como "zero", e
// carta sem k!wi ainda nao tem valor — sao coisas diferentes.
function medidor(valor, maximo, titulo) {
  const v = n(valor);
  if (v === null) return "";
  const pct = maximo > 0 ? Math.max(v > 0 ? 2 : 0, Math.round(v / maximo * 100)) : 0;
  return `<span class="medidor" title="${esc(titulo || "")}"><i style="width:${pct}%"></i></span>`;
}
const pontoConta = (id) => `<span class="ponto ${esc(id)}" aria-hidden="true"></span>`;

/* ======================================================================
   Graficos: primitivas SVG
   ====================================================================== */
function tetoBonito(v) {
  if (!v || v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const r = v / p;
  return (r <= 1 ? 1 : r <= 2 ? 2 : r <= 2.5 ? 2.5 : r <= 5 ? 5 : 10) * p;
}
// Barra com as duas quinas de cima arredondadas e a base reta na linha zero.
function topoArredondado(x, y, w, h, r) {
  const rr = Math.max(0, Math.min(r, h, w / 2));
  return `M${x},${y + h} V${y + rr} Q${x},${y} ${x + rr},${y} `
       + `H${x + w - rr} Q${x + w},${y} ${x + w},${y + rr} V${y + h} Z`;
}
function pontaArredondada(x, y, w, h, r) {
  const rr = Math.max(0, Math.min(r, w, h / 2));
  return `M${x},${y} H${x + w - rr} Q${x + w},${y} ${x + w},${y + rr} `
       + `V${y + h - rr} Q${x + w},${y + h} ${x + w - rr},${y + h} H${x} Z`;
}

const dica = {
  mostrar(ev, titulo, linhasDica) {
    const el = $("#dica");
    el.innerHTML = `<div class="t">${esc(titulo)}</div>` + linhasDica.map((l) =>
      `<div class="l">${l.cor ? `<span class="ponto" style="background:${l.cor}"></span>` : ""}`
      + `${esc(l.rot)} <b>${fmt(l.valor)}</b></div>`).join("");
    el.style.opacity = "1";
    const r = el.getBoundingClientRect();
    const x = Math.min(ev.clientX + 14, window.innerWidth - r.width - 8);
    const y = Math.max(8, ev.clientY - r.height - 12);
    el.style.left = `${x}px`;
    el.style.top = `${y}px`;
  },
  esconder() { $("#dica").style.opacity = "0"; },
};

/**
 * Colunas (empilhadas quando ha mais de uma serie).
 * bandas: [{ rot, curto, valores: {serieId: n} }]
 * series: [{ id, rot, cor }]
 */
function colunas(host, { bandas, series, passoRotulo = 1, altura = 190 }) {
  const W = Math.max(260, host.clientWidth);
  const H = altura, m = { t: 12, r: 8, b: 24, l: 46 };
  const larg = W - m.l - m.r, alt = H - m.t - m.b;
  const totais = bandas.map((b) => series.reduce((t, s) => t + (b.valores[s.id] || 0), 0));
  const teto = tetoBonito(Math.max(...totais, 0));
  const y = (v) => m.t + alt - (v / teto) * alt;
  const banda = larg / bandas.length;
  const bw = Math.min(24, Math.max(3, banda - 6));

  let g = "";
  // grade recessiva + ticks arredondados
  [0, teto / 2, teto].forEach((v) => {
    g += `<line class="g-grade" x1="${m.l}" y1="${y(v)}" x2="${W - m.r}" y2="${y(v)}"/>`
      + `<text class="g-eixo" x="${m.l - 8}" y="${y(v) + 4}" text-anchor="end">${compacto(v)}</text>`;
  });

  let barras = "", eixoX = "", faixas = "";
  bandas.forEach((b, i) => {
    const cx = m.l + banda * i + (banda - bw) / 2;
    let acc = 0;
    // de baixo pra cima; 2px de superficie separando cada segmento
    const pilha = series.map((s) => ({ s, v: b.valores[s.id] || 0 })).filter((p) => p.v > 0);
    pilha.forEach((p, k) => {
      const yTopo = y(acc + p.v), yBase = y(acc);
      const h = Math.max(1, yBase - yTopo - (k < pilha.length - 1 ? 0 : 0) - (k > 0 ? 2 : 0));
      const yy = yTopo + (k > 0 ? 2 : 0);
      const ultimo = k === pilha.length - 1;
      barras += ultimo
        ? `<path d="${topoArredondado(cx, yy, bw, h, 4)}" fill="${p.s.cor}"/>`
        : `<rect x="${cx}" y="${yy}" width="${bw}" height="${h}" fill="${p.s.cor}"/>`;
      acc += p.v;
    });
    if (i % passoRotulo === 0) {
      eixoX += `<text class="g-eixo" x="${m.l + banda * i + banda / 2}" y="${H - 6}"
        text-anchor="middle">${esc(b.curto)}</text>`;
    }
    faixas += `<rect class="g-faixa" x="${m.l + banda * i}" y="${m.t}" width="${banda}"
      height="${alt}" fill="transparent" data-i="${i}"/>`;
  });

  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img">`
    + g + barras + eixoX + faixas + `</svg>`;

  host.querySelectorAll(".g-faixa").forEach((el) => {
    el.addEventListener("mousemove", (ev) => {
      const b = bandas[Number(el.dataset.i)];
      const ls = series.map((s) => ({ rot: s.rot, cor: s.cor, valor: b.valores[s.id] || 0 }))
        .filter((l) => l.valor > 0);
      dica.mostrar(ev, b.rot, ls.length ? ls : [{ rot: "nada", valor: 0 }]);
    });
    el.addEventListener("mouseleave", dica.esconder);
  });
}

/** Barras horizontais com rotulo direto no tip. */
function barrasH(host, { itens, cor, sufixo = "" }) {
  const W = Math.max(260, host.clientWidth);
  const linhaAlt = 26, m = { t: 4, r: 56, b: 4, l: 150 };
  const H = m.t + m.b + itens.length * linhaAlt;
  const larg = W - m.l - m.r;
  const teto = tetoBonito(Math.max(...itens.map((i) => i.valor), 0));
  let g = "";
  itens.forEach((it, i) => {
    const yy = m.t + i * linhaAlt + 6;
    const w = Math.max(2, (it.valor / teto) * larg);
    g += `<text class="g-eixo" x="${m.l - 10}" y="${yy + 11}" text-anchor="end">${esc(corta(it.rot, 22))}</text>`
      + `<path d="${pontaArredondada(m.l, yy, w, 14, 4)}" fill="${cor}"/>`
      + `<text class="g-rotulo" x="${m.l + w + 8}" y="${yy + 11}">${fmt(it.valor)}${esc(sufixo)}</text>`
      + `<rect class="g-faixa" x="0" y="${m.t + i * linhaAlt}" width="${W}" height="${linhaAlt}"
           fill="transparent" data-i="${i}"/>`;
  });
  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img">${g}</svg>`;
  host.querySelectorAll(".g-faixa").forEach((el) => {
    el.addEventListener("mousemove", (ev) => {
      const it = itens[Number(el.dataset.i)];
      dica.mostrar(ev, it.rot, it.detalhe || [{ rot: "total", valor: it.valor }]);
    });
    el.addEventListener("mouseleave", dica.esconder);
  });
}

/* ======================================================================
   Visao geral
   ====================================================================== */
const chaveHora = (d) => d.toISOString().slice(0, 13);
const chaveDia = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-`
  + String(d.getDate()).padStart(2, "0");

function seriesContas() {
  const ativas = conta ? [conta] : CONTAS;
  return ativas.map((c) => ({ id: c, rot: `conta ${c}`, cor: corConta(c) }));
}

function bandasTempo() {
  const g = dados.graficos || {};
  const porHora = g.por_hora || [];
  if (porHora.length) {
    const base = new Date(); base.setMinutes(0, 0, 0);
    const bandas = [];
    for (let i = 23; i >= 0; i--) {
      const d = new Date(base.getTime() - i * 3600e3);
      bandas.push({
        chave: chaveHora(d), valores: {},
        curto: String(d.getHours()).padStart(2, "0") + "h",
        rot: d.toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit" }) + "h",
      });
    }
    const idx = Object.fromEntries(bandas.map((b, i) => [b.chave, i]));
    porHora.forEach((r) => {
      const i = idx[chaveHora(new Date(r.t))];
      if (i !== undefined) bandas[i].valores[r.conta_id] = Number(r.n);
    });
    return { bandas, passo: 3, titulo: "cartas pegas em drop nas ultimas 24h", unidade: "hora" };
  }
  // Sem nada nas ultimas 24h o grafico de hora fica vazio; cai pros dias.
  const bandas = [];
  const hoje = new Date();
  for (let i = 13; i >= 0; i--) {
    const d = new Date(hoje.getFullYear(), hoje.getMonth(), hoje.getDate() - i);
    bandas.push({
      chave: chaveDia(d), valores: {},
      curto: `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")}`,
      rot: d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit", year: "numeric" }),
    });
  }
  const idx = Object.fromEntries(bandas.map((b, i) => [b.chave, i]));
  (g.por_dia || []).forEach((r) => {
    const i = idx[String(r.t).slice(0, 10)];
    if (i !== undefined) bandas[i].valores[r.conta_id] = Number(r.n);
  });
  return { bandas, passo: 2, titulo: "cartas pegas em drop nos ultimos 14 dias", unidade: "dia" };
}

function cartaGrafico(id, titulo, sub, largo) {
  return `<section class="grafico${largo ? " largo" : ""}">
    <h2>${esc(titulo)}</h2>
    <p class="legenda-topo">${esc(sub)}</p>
    <div class="alvo" id="${id}"></div>
  </section>`;
}

function legendaContas(host, totais) {
  const s = seriesContas().filter((x) => (totais[x.id] || 0) > 0);
  if (s.length < 2) return "";   // uma serie so: o titulo ja diz o que eh
  return `<div class="legenda">${s.map((x) =>
    `<span>${pontoConta(x.id)} ${esc(x.rot)} <b>${fmt(totais[x.id])}</b></span>`).join("")}</div>`;
}

function visaoGeral() {
  const g = (dados && dados.graficos) || {};
  const tempo = bandasTempo();
  const temTempo = tempo.bandas.some((b) => Object.keys(b.valores).length);
  const temEstrelas = (g.estrelas || []).length > 0;
  const temJob = (g.faixas_job || []).length > 0;
  const temSeries = (g.top_series || []).length > 0;

  $("#conteudo").innerHTML = `<div class="graficos">
    ${cartaGrafico("g-contas", "cartas por conta", "participacao de cada conta no total", true)}
    ${cartaGrafico("g-tempo", "ritmo de pick", tempo.titulo, true)}
    ${cartaGrafico("g-estrelas", "distribuicao de estrelas", "todas as cartas, por conta")}
    ${cartaGrafico("g-job", "esforco das cartas job", "quantas cartas em cada faixa de k!wi")}
    ${cartaGrafico("g-series", "series mais desejadas", "soma de desejos entre as cartas desejadas", true)}
  </div>`;

  // --- cartas por conta: barra empilhada unica, com rotulos diretos
  const rs = (dados ? dados.resumo : []).filter((r) => Number(r.total) > 0
    && (!conta || r.conta_id === conta));
  const totalC = rs.reduce((t, r) => t + Number(r.total), 0);
  $("#g-contas").innerHTML = totalC ? `<div class="empilhada">
      <div class="faixa">${rs.map((r) =>
        `<i style="width:${Number(r.total) / totalC * 100}%;background:${corConta(r.conta_id)}"
            title="conta ${r.conta_id}: ${fmt(r.total)}"></i>`).join("")}</div>
      <div class="legenda">${rs.map((r) =>
        `<span>${pontoConta(r.conta_id)} conta ${r.conta_id} <b>${fmt(r.total)}</b>
          <span class="fraco">${Math.round(Number(r.total) / totalC * 100)}%</span></span>`).join("")}
      </div></div>` : `<div class="vazio-g">sem cartas ainda</div>`;

  // --- ritmo de entrada
  if (temTempo) {
    colunas($("#g-tempo"), { bandas: tempo.bandas, series: seriesContas(), passoRotulo: tempo.passo });
    const totais = {};
    tempo.bandas.forEach((b) => CONTAS.forEach((c) =>
      (totais[c] = (totais[c] || 0) + (b.valores[c] || 0))));
    $("#g-tempo").insertAdjacentHTML("beforeend", legendaContas($("#g-tempo"), totais));
  } else {
    $("#g-tempo").innerHTML = `<div class="vazio-g">nenhum drop nesse periodo</div>`;
  }

  // --- estrelas
  if (temEstrelas) {
    const bandas = [0, 1, 2, 3, 4, 5].map((e) => ({
      chave: e, valores: {},
      curto: e === 0 ? "0" : "★".repeat(e),
      rot: e === 1 ? "1 estrela" : `${e} estrelas`,
    }));
    const totais = {};
    g.estrelas.forEach((r) => {
      const b = bandas[Number(r.estrelas)];
      if (b) b.valores[r.conta_id] = Number(r.n);
      totais[r.conta_id] = (totais[r.conta_id] || 0) + Number(r.n);
    });
    colunas($("#g-estrelas"), { bandas, series: seriesContas() });
    $("#g-estrelas").insertAdjacentHTML("beforeend", legendaContas($("#g-estrelas"), totais));
  } else {
    $("#g-estrelas").innerHTML = `<div class="vazio-g">sem cartas</div>`;
  }

  // --- faixas de esforco (serie unica: sem legenda, o titulo ja diz)
  if (temJob) {
    const bandas = g.faixas_job.map((r) => {
      const f = Number(r.faixa);
      return {
        chave: f, valores: { job: Number(r.n) },
        curto: f >= 200 ? "200+" : String(f),
        rot: f >= 200 ? "esforco 200 ou mais" : `esforco ${f} a ${f + 9}`,
      };
    });
    colunas($("#g-job"), {
      bandas, series: [{ id: "job", rot: "cartas", cor: "var(--barra)" }],
      passoRotulo: bandas.length > 10 ? 2 : 1,
    });
  } else {
    $("#g-job").innerHTML = `<div class="vazio-g">nenhuma carta classificada como job</div>`;
  }

  // --- top series
  if (temSeries) {
    barrasH($("#g-series"), {
      cor: "var(--barra)",
      itens: g.top_series.map((r) => ({
        rot: r.serie, valor: Number(r.desejos),
        detalhe: [{ rot: "desejos", valor: Number(r.desejos) },
                  { rot: "cartas", valor: Number(r.cartas) }],
      })),
    });
  } else {
    $("#g-series").innerHTML = `<div class="vazio-g">nenhuma carta desejada ainda</div>`;
  }

  $("#rodape").textContent = conta
    ? `numeros da conta ${conta}, calculados sobre as tabelas inteiras`
    : "numeros das tres contas, calculados sobre as tabelas inteiras";
}

/* ======================================================================
   Navegacao lateral
   ====================================================================== */
function pintarNav() {
  const porConta = {};
  (dados ? dados.resumo : []).forEach((r) => (porConta[r.conta_id] = r));
  const total = (dados ? dados.resumo : []).reduce((t, r) => t + (Number(r.total) || 0), 0);

  $("#nav-contas").innerHTML = [
    `<button data-conta="" aria-pressed="${conta === ""}">
       <span class="ponto todas"></span><span class="rotulo">todas</span>
       <span class="cont">${fmt(total)}</span></button>`,
    ...CONTAS.map((c) => `
      <button data-conta="${c}" aria-pressed="${conta === c}">
        ${pontoConta(c)}<span class="rotulo">conta ${c}</span>
        <span class="cont">${fmt(porConta[c] ? porConta[c].total : 0)}</span></button>`),
  ].join("");

  const cont = { cartas: soma("total"), job: soma("job"), desejadas: soma("desejadas") };
  $("#nav-secoes").innerHTML = SECOES.map((s) => `
    <button data-secao="${s.id}" aria-pressed="${secao === s.id}">
      <span class="rotulo">${s.rotulo}</span>
      <span class="cont">${s.id === "geral" ? "" : fmt(cont[s.id])}</span></button>`).join("");
}

/* ======================================================================
   Resumo: heroi + tiles
   ====================================================================== */
function pintarResumo() {
  const avaliadas = soma("avaliadas"), total = soma("total");
  const pend = soma("pendentes"), pctAv = total ? Math.round(avaliadas / total * 100) : 0;
  const rs = resumoSel();
  const melhorEsf = Math.max(0, ...rs.map((r) => Number(r.melhor_esforco) || 0));
  const maisDes = Math.max(0, ...rs.map((r) => Number(r.mais_desejada) || 0));
  const ultima = rs.map((r) => r.ultima_carta).filter(Boolean).sort().pop();

  $("#heroi-valor").textContent = compacto(total);
  $("#heroi-rotulo").textContent = conta ? `cartas na conta ${conta}` : "cartas em todas as contas";
  $("#heroi-sub").textContent = ultima ? `ultima entrou ${fmtData(ultima)}` : "";

  $("#tiles").innerHTML = `
    <div class="tile">
      <span class="rot">avaliadas</span>
      <span class="val">${compacto(avaliadas)}</span>
      ${medidor(avaliadas, total, `${pctAv}% avaliadas`)}
      <span class="nota">${pctAv}% do total</span>
    </div>
    <div class="tile">
      <span class="rot">pendentes de k!wi</span>
      <span class="val">${compacto(pend)}</span>
      <span class="nota">${pend ? "fila do avaliador" : "fila vazia"}</span>
    </div>
    <div class="tile">
      <span class="rot">job</span>
      <span class="val">${compacto(soma("job"))}</span>
      <span class="nota">boas pra trabalhar</span>
    </div>
    <div class="tile">
      <span class="rot">desejadas</span>
      <span class="val">${compacto(soma("desejadas"))}</span>
      <span class="nota">alta wishlist</span>
    </div>
    <div class="tile">
      <span class="rot">melhor esforco</span>
      <span class="val">${fmt(melhorEsf || null)}</span>
      <span class="nota">maior k!wi</span>
    </div>
    <div class="tile">
      <span class="rot">mais desejada</span>
      <span class="val">${compacto(maisDes || null)}</span>
      <span class="nota">maior wishlist</span>
    </div>`;
}

/* ======================================================================
   Filtro / ordenacao / chips
   ====================================================================== */
function chipsDaSecao() { return CHIPS[secao] || []; }

function pintarChips() {
  if (!ehTabela()) { $("#chips").innerHTML = ""; return; }
  const base = linhas();
  const cont = (dados && dados.contagens) || {};
  $("#chips").innerHTML = chipsDaSecao().map((c) => {
    // Filtro de servidor: a contagem vem do banco (total real da tabela).
    // Filtro local: conta nas linhas carregadas mesmo.
    const k = c.servidor ? cont[c.id] : base.filter(c.f).length;
    return `<button data-chip="${c.id}" aria-pressed="${chip === c.id}">
      ${esc(c.rot)}<span class="cnt">${fmt(k)}</span></button>`;
  }).join("");
}

function filtradas() {
  const q = $("#busca").value.trim().toLowerCase();
  const cf = chipsDaSecao().find((c) => c.id === chip);
  let ls = linhas();
  if (cf && cf.f) ls = ls.filter(cf.f);   // os de servidor ja vieram filtrados
  if (q) {
    ls = ls.filter((l) => [l.codigo, l.personagem, l.serie, l.condicao, l.motivo, l.origem]
      .some((c) => c && String(c).toLowerCase().includes(q)));
  }
  if (!ordem) return ls;
  const { chave, tipo, desc } = ordem;
  return [...ls].sort((x, y) => {
    let a = x[chave], b = y[chave];
    if (a === null || a === undefined) return 1;      // vazio sempre no fim
    if (b === null || b === undefined) return -1;
    if (TIPO_NUM.has(tipo)) { a = Number(a); b = Number(b); }
    else if (tipo === "data") { a = Date.parse(a) || 0; b = Date.parse(b) || 0; }
    else { a = String(a).toLowerCase(); b = String(b).toLowerCase(); }
    return (a < b ? -1 : a > b ? 1 : 0) * (desc ? -1 : 1);
  });
}

/* ======================================================================
   Visao: tabela densa
   ====================================================================== */
function celula(l, [chave, , tipo], maxMetrica) {
  if (tipo === "classe") {
    const t = [];
    if (l.eh_job) t.push('<span class="selo job">job</span>');
    if (l.eh_desejada) t.push('<span class="selo des">desejada</span>');
    return `<td>${t.join("") || '<span class="fraco">—</span>'}</td>`;
  }
  const v = l[chave];
  switch (tipo) {
    case "conta":
      return `<td><span class="cel-conta">${pontoConta(v)}${esc(v)}</span></td>`;
    case "codigo": return `<td class="codigo">${esc(v)}</td>`;
    case "nome": return `<td class="nome">${v ? esc(v) : '<span class="fraco">—</span>'}</td>`;
    case "serie": return `<td class="serie">${v ? esc(v) : '<span class="fraco">—</span>'}</td>`;
    case "data": return `<td class="fraco">${fmtData(v)}</td>`;
    case "estrelas":
      return `<td class="estrelas">${v ? "★".repeat(v) : '<span class="fraco">—</span>'}</td>`;
    case "print": return `<td class="num">${v ? "#" + fmt(v) : '<span class="fraco">—</span>'}</td>`;
    case "num": return `<td class="num">${v === null || v === undefined
      ? '<span class="fraco">—</span>' : fmt(v)}</td>`;
    case "barra":
      if (n(v) === null) return `<td class="num"><span class="fraco">—</span></td>`;
      return `<td><span class="cel-barra">${medidor(v, maxMetrica, `${fmt(v)}`)}
        <span class="v">${fmt(v)}</span></span></td>`;
    default: return `<td>${v ? esc(v) : '<span class="fraco">—</span>'}</td>`;
  }
}

function visaoTabela(ls) {
  const cols = COLUNAS[secao];
  const maxM = maxDe(secaoAtual().metrica);
  const cab = cols.map(([chave, rot, tipo]) => {
    const seta = ordem && ordem.chave === chave
      ? `<span class="seta">${ordem.desc ? "▾" : "▴"}</span>` : "";
    return `<th data-chave="${chave}" data-tipo="${tipo}" title="ordenar por ${esc(rot)}">${esc(rot)} ${seta}</th>`;
  }).join("");
  const corpo = ls.length
    ? ls.map((l) => "<tr>" + cols.map((c) => celula(l, c, maxM)).join("") + "</tr>").join("")
    : `<tr><td class="vazio" colspan="${cols.length}">nada por aqui com esse filtro</td></tr>`;
  return `<div class="quadro"><table id="tabela">
      <thead><tr>${cab}</tr></thead><tbody>${corpo}</tbody></table></div>`;
}

/* ======================================================================
   Visao: galeria
   ====================================================================== */
function visaoGaleria(ls) {
  if (!ls.length) return `<div class="quadro"><div class="vazio">nada por aqui com esse filtro</div></div>`;
  const maxEsf = Math.max(1, maxDe("esforco_total"));
  const maxDes = Math.max(1, maxDe("desejos"));
  // Galeria eh pra olhar, nao pra varrer 500 linhas; o resto fica na tabela.
  const corte = ls.slice(0, 240);
  return `<div class="galeria">${corte.map((l) => `
    <article class="carta c${esc(l.conta_id)}">
      <div class="cab">
        <span class="cel-conta">${pontoConta(l.conta_id)}${esc(l.conta_id)}</span>
        ${secao === "cartas" && l.eh_job ? '<span class="selo job">job</span>' : ""}
        ${secao === "cartas" && l.eh_desejada ? '<span class="selo des">desejada</span>' : ""}
        <span class="codigo">${esc(l.codigo)}</span>
      </div>
      <div>
        <div class="titulo" title="${esc(l.personagem || "")}">${l.personagem ? esc(l.personagem) : "<span class='fraco'>sem nome ainda</span>"}</div>
        <div class="sub" title="${esc(l.serie || "")}">${l.serie ? esc(l.serie) : "<span class='fraco'>serie desconhecida</span>"}</div>
      </div>
      <div class="ficha">
        <span class="estrelas">${l.estrelas ? "★".repeat(l.estrelas) : "—"}</span>
        <span>#${fmt(l.numero_print)}</span>
        <span>ed ${fmt(l.edicao)}</span>
        <span>${l.condicao ? esc(l.condicao) : "—"}</span>
      </div>
      <div class="metricas">
        <div class="metrica"><span>esforco</span>${medidor(l.esforco_total, maxEsf, `esforco ${fmt(l.esforco_total)}`)}<span class="v">${fmt(l.esforco_total)}</span></div>
        <div class="metrica"><span>desejos</span>${medidor(l.desejos, maxDes, `desejos ${fmt(l.desejos)}`)}<span class="v">${fmt(l.desejos)}</span></div>
      </div>
    </article>`).join("")}</div>`;
}

/* ======================================================================
   Visao: colunas por conta
   ====================================================================== */
function visaoColunas(ls) {
  const alvo = conta ? [conta] : CONTAS;
  const met = secaoAtual().metrica, metRot = secaoAtual().metricaRot;
  const maxM = Math.max(1, maxDe(met));
  return `<div class="colunas">${alvo.map((c) => {
    const minhas = ls.filter((l) => l.conta_id === c)
      .sort((x, y) => (Number(y[met]) || 0) - (Number(x[met]) || 0));
    const corpo = minhas.length ? minhas.slice(0, 150).map((l) => `
      <div class="item">
        <span class="n" title="${esc(l.personagem || l.codigo)}">${l.personagem ? esc(l.personagem) : `<span class="codigo">${esc(l.codigo)}</span>`}</span>
        <span class="s">${l.serie ? esc(l.serie) : '<span class="fraco">—</span>'} · ${l.estrelas ? `<span class="estrelas">${"★".repeat(l.estrelas)}</span>` : "—"}</span>
        <span class="m"><span class="v">${fmt(l[met])}</span>${medidor(l[met], maxM, `${metRot} ${fmt(l[met])}`)}</span>
      </div>`).join("") : `<div class="vazio">sem cartas aqui</div>`;
    return `<section class="coluna">
      <header>${pontoConta(c)} conta ${c}
        <span class="cont">${fmt(minhas.length)} · top ${esc(metRot)}</span></header>
      <div class="lista">${corpo}</div>
    </section>`;
  }).join("")}</div>`;
}

/* ======================================================================
   Pintura geral
   ====================================================================== */
function pintarConteudo() {
  if (!dados) return;
  if (!ehTabela()) { visaoGeral(); return; }

  const ls = filtradas();
  $("#conteudo").innerHTML = visao === "galeria" ? visaoGaleria(ls)
    : visao === "colunas" ? visaoColunas(ls)
    : visaoTabela(ls);

  const tab = secaoAtual().tabela;
  const corte = dados.truncado && dados.truncado[secao]
    ? ` · limite de ${dados.limite} atingido, aumente em "limite" pra ver o resto` : "";
  const extra = visao === "galeria" && ls.length > 240 ? " · galeria mostra as 240 primeiras" : "";
  $("#rodape").textContent =
    `${fmt(ls.length)} de ${fmt(linhas().length)} linhas carregadas de ${tab}${extra}${corte}`;
}

function pintar() {
  if (!dados) return;
  pintarNav();
  pintarResumo();
  pintarChips();
  pintarConteudo();
  $("#titulo").firstChild.textContent = secaoAtual().rotulo;
  $("#subtitulo").textContent = conta ? `conta ${conta}` : "todas as contas";
  document.title = `Karuta — ${secaoAtual().rotulo}${conta ? " · conta " + conta : ""}`;
  // Na visao geral nao ha linhas pra filtrar nem visao alternativa.
  $("#bloco-visao").classList.toggle("oculto", !ehTabela());
  $("#visoes-topo").classList.toggle("oculto", !ehTabela());
  $(".busca").classList.toggle("oculto", !ehTabela());
}

/* ======================================================================
   Dados
   ====================================================================== */
async function carregar() {
  if (carregando) return;
  carregando = true;
  $("#pulso").classList.add("off");
  $("#estado").textContent = "atualizando…";
  try {
    const cf = chipsDaSecao().find((c) => c.id === chip);
    const filtro = cf && cf.servidor ? chip : "todas";
    const r = await fetch(
      `/api/dados?conta=${conta}&limite=${$("#limite").value}&filtro=${filtro}`);
    const j = await r.json();
    if (!r.ok) throw new Error(j.erro || r.statusText);
    dados = j;
    ultimo = new Date();
    $("#erro").style.display = "none";
    pintar();
  } catch (e) {
    $("#erro").style.display = "block";
    $("#erro").innerHTML = `<b>banco indisponivel.</b> ${esc(e.message)} — tento de novo no proximo refresh.`;
  } finally {
    carregando = false;
    $("#pulso").classList.remove("off");
    $("#estado").textContent = ultimo
      ? `atualizado ${ultimo.toLocaleTimeString("pt-BR")}` : "sem dados";
  }
}

function agendar() {
  if (timer) clearInterval(timer);
  const s = Number($("#auto").value);
  if (s > 0) timer = setInterval(carregar, s * 1000);
}

/* ======================================================================
   Eventos
   ====================================================================== */
function marcar(sel, attr, valor) {
  document.querySelectorAll(`${sel} button`).forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset[attr] === valor)));
}

$("#nav-contas").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  conta = b.dataset.conta; carregar();
});
$("#nav-secoes").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  secao = b.dataset.secao; ordem = null; chip = "todas";
  // O chip volta pra "todas", mas as cartas em memoria podem ter vindo
  // filtradas do servidor; nesse caso precisa reconsultar.
  if (dados && dados.filtro && dados.filtro !== "todas") carregar();
  else pintar();
});
document.querySelectorAll("#nav-visoes, #visoes-topo").forEach((el) =>
  el.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    visao = b.dataset.visao;
    marcar("#nav-visoes", "visao", visao);
    marcar("#visoes-topo", "visao", visao);
    pintarConteudo();
  }));
$("#chips").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  chip = b.dataset.chip;
  marcar("#chips", "chip", chip);
  const def = chipsDaSecao().find((c) => c.id === chip);
  if (def && def.servidor) carregar();   // refaz a consulta com o filtro novo
  else pintarConteudo();
});
$("#conteudo").addEventListener("click", (e) => {
  const th = e.target.closest("th"); if (!th) return;
  const chave = th.dataset.chave;
  ordem = ordem && ordem.chave === chave
    ? { chave, tipo: th.dataset.tipo, desc: !ordem.desc }
    : { chave, tipo: th.dataset.tipo, desc: true };
  pintarConteudo();
});
$("#busca").addEventListener("input", () => pintarConteudo());
$("#limite").addEventListener("change", carregar);
$("#auto").addEventListener("change", agendar);
$("#recarregar").addEventListener("click", carregar);
$("#tema").addEventListener("change", (e) => {
  const v = e.target.value;
  if (v === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", v);
  try { localStorage.setItem("karuta-tema", v); } catch (_) { /* modo anonimo */ }
});
// SVG eh desenhado em pixel medido, entao precisa ser refeito ao redimensionar.
window.addEventListener("resize", () => {
  if (ehTabela()) return;
  clearTimeout(redesenho);
  redesenho = setTimeout(pintarConteudo, 150);
});

// tema salvo antes do primeiro fetch, pra nao piscar branco no escuro
try {
  const t = localStorage.getItem("karuta-tema");
  if (t && t !== "auto") { document.documentElement.setAttribute("data-theme", t); $("#tema").value = t; }
} catch (_) { /* modo anonimo */ }

carregar();
agendar();

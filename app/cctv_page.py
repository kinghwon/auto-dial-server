"""CCTV 웹 뷰어 화면(로그인 창 + 반응형 뷰어).

외부 CSS·JS 없이 한 파일로 끝나게 해서 폰 브라우저에서도 가볍게 뜨도록 했다.
뷰어는 여러 대의 폰(기기)을 탭으로 골라 보거나 '한눈에 보기(그리드)'로 동시에 본다.
"""
from __future__ import annotations

LOGIN_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>초경량 CCTV · 로그인</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: #0f1115; color: #e8eaed;
    font-family: -apple-system, BlinkMacSystemFont, "Malgun Gothic", "Apple SD Gothic Neo", sans-serif;
    padding: 24px;
  }
  .card {
    width: 100%; max-width: 360px; background: #171a21; border: 1px solid #262b36;
    border-radius: 16px; padding: 26px 22px; box-shadow: 0 10px 40px rgba(0,0,0,.45);
  }
  h1 { font-size: 19px; margin: 0 0 6px; letter-spacing: -.3px; }
  p.sub { margin: 0 0 20px; font-size: 13px; color: #9aa3b2; line-height: 1.5; }
  label { display: block; font-size: 12px; color: #9aa3b2; margin: 14px 0 6px; }
  input {
    width: 100%; padding: 13px 14px; font-size: 16px; border-radius: 10px;
    border: 1px solid #2d3340; background: #0f131a; color: #e8eaed; outline: none;
  }
  input:focus { border-color: #4c8dff; }
  button {
    width: 100%; margin-top: 20px; padding: 14px; font-size: 16px; font-weight: 700;
    border: 0; border-radius: 10px; background: #3b82f6; color: #fff; cursor: pointer;
  }
  button:active { background: #2f6fd8; }
  .err {
    margin-top: 16px; padding: 11px 13px; border-radius: 10px; font-size: 13px; line-height: 1.45;
    background: #3a1620; border: 1px solid #6b2130; color: #ffb4c0;
  }
  .lock { font-size: 22px; margin-bottom: 10px; }
</style>
</head>
<body>
  <form class="card" method="post" action="/cctv/login">
    <div class="lock">🔒</div>
    <h1>초경량 CCTV</h1>
    <p class="sub">이 화면은 비밀번호를 아는 사람만 볼 수 있습니다.</p>
    <label for="u">아이디</label>
    <input id="u" name="username" type="text" value="admin" autocomplete="username" autocapitalize="off">
    <label for="p">비밀번호</label>
    <input id="p" name="password" type="password" autocomplete="current-password" autofocus required>
    <button type="submit">들어가기</button>
    __ERROR__
  </form>
</body>
</html>
"""


VIEWER_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#0f1115">
<title>초경량 CCTV</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { height: 100%; }
  body {
    margin: 0; background: #0b0d11; color: #e8eaed;
    font-family: -apple-system, BlinkMacSystemFont, "Malgun Gothic", "Apple SD Gothic Neo", sans-serif;
    display: flex; flex-direction: column;
  }
  header {
    display: flex; align-items: center; gap: 10px; padding: 12px 14px;
    background: #12151c; border-bottom: 1px solid #212734; flex-wrap: wrap;
    flex: 0 0 auto;            /* 본문이 길어도 헤더·탭 줄이 찌그러지지 않게 */
  }
  header h1 { font-size: 15px; margin: 0; font-weight: 700; letter-spacing: -.3px; white-space: nowrap; }
  .pill {
    display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: #b8c0cd;
    background: #1a1f29; border: 1px solid #28303d; border-radius: 999px; padding: 5px 10px;
    white-space: nowrap;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #6b7280; }
  .dot.on { background: #22c55e; box-shadow: 0 0 0 3px rgba(34,197,94,.16); }
  .dot.off { background: #ef4444; box-shadow: 0 0 0 3px rgba(239,68,68,.14); }
  .spacer { flex: 1; }
  .linkbtn {
    background: none; border: 1px solid #2b3341; color: #b8c0cd; border-radius: 8px;
    padding: 7px 11px; font-size: 12px; cursor: pointer;
  }
  .linkbtn.on { background: #1d2a44; border-color: #3559a8; color: #cfe0ff; }

  /* 기기 선택 탭 */
  .tabs {
    display: flex; gap: 8px; padding: 10px 12px 0; overflow-x: auto;
    -webkit-overflow-scrolling: touch; flex: 0 0 auto; min-height: 40px;
  }
  .tabs::-webkit-scrollbar { display: none; }
  .tab {
    display: inline-flex; align-items: center; gap: 7px; flex: 0 0 auto; cursor: pointer;
    background: #131822; border: 1px solid #28303d; color: #c3cbd8;
    border-radius: 999px; padding: 9px 13px; font-size: 13px; font-weight: 700; white-space: nowrap;
  }
  .tab.on { background: #1b2b4d; border-color: #3b6bd6; color: #eaf1ff; }
  .tab.gridbtn { border-style: dashed; }

  main { flex: 1 0 auto; display: flex; flex-direction: column; gap: 10px; padding: 10px 12px 18px; }
  .stage {
    position: relative; width: 100%; background: #000; border-radius: 14px; overflow: hidden;
    border: 1px solid #1c2230; min-height: 220px;
    display: flex; align-items: center; justify-content: center;
  }
  /* 회전(90°/270°)에서도 잘리지 않게 JS 가 폭·높이(px)와 stage 높이를 맞춰 준다. */
  .stage img { display: block; transform-origin: center center; transition: transform .18s ease; }
  .stage .hint {
    position: absolute; inset: 0; display: flex; flex-direction: column; gap: 8px;
    align-items: center; justify-content: center; text-align: center; padding: 20px;
    color: #8d97a7; font-size: 13px; line-height: 1.6; background: #0a0c10;
  }
  .stage .hint b { color: #cfd6e2; font-size: 14px; }
  @media (min-width: 900px) { main { max-width: 1100px; margin: 0 auto; width: 100%; } }

  /* 한눈에 보기(그리드) */
  .gridview { display: none; gap: 8px; grid-template-columns: 1fr; }
  .gridview.show { display: grid; }
  @media (min-width: 700px) { .gridview.show { grid-template-columns: 1fr 1fr; } }
  .tile {
    position: relative; background: #000; border: 1px solid #1c2230; border-radius: 12px;
    overflow: hidden; aspect-ratio: 4 / 3; min-height: 180px;   /* aspect-ratio 를 모르는 예전 브라우저용 */
  }
  .tile img {
    width: 100%; height: 100%; object-fit: contain; display: block; cursor: pointer;
    transition: transform .18s ease;
  }
  .tile .tlabel {
    position: absolute; left: 0; right: 0; bottom: 0; padding: 6px 9px; font-size: 12px;
    background: linear-gradient(transparent, rgba(0,0,0,.72)); color: #e6ecf5; font-weight: 700;
  }
  body.grid main .stage, body.grid main .single { display: none; }

  .controls { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .controls.fpsrow { grid-template-columns: repeat(6, 1fr); }
  .controls button {
    padding: 13px 8px; font-size: 13.5px; font-weight: 700; border-radius: 11px;
    border: 1px solid #2a3241; background: #161b24; color: #dfe5ee; cursor: pointer;
    display: flex; align-items: center; justify-content: center; gap: 6px; min-height: 46px;
  }
  .controls button:active { background: #202734; }
  .controls button.on { background: #1c3a2a; border-color: #2a6b45; color: #a7f3c6; }
  .controls button:disabled { opacity: .45; cursor: default; }
  .controls button:disabled:active { background: #161b24; }
  .row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12.5px; color: #93a0b4; }
  .row .val { color: #dfe5ee; font-weight: 600; }
  .card {
    background: #10141b; border: 1px solid #1e2531; border-radius: 13px; padding: 13px 14px;
  }
  .card h2 { font-size: 13px; margin: 0 0 10px; color: #cfd6e2; }
  .card p { margin: 0 0 10px; font-size: 12.5px; color: #93a0b4; line-height: 1.55; }
  .card input {
    width: 100%; padding: 11px 12px; font-size: 15px; border-radius: 9px; margin-bottom: 8px;
    border: 1px solid #2a3241; background: #0b0f15; color: #e8eaed;
  }
  .card button.act {
    padding: 11px 14px; font-size: 13px; font-weight: 700; border-radius: 9px; border: 1px solid #2a3241;
    background: #1b2431; color: #dfe5ee; cursor: pointer;
  }
  .card button.act.primary { background: #2563eb; border-color: #2563eb; color: #fff; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .msg { font-size: 12.5px; margin-top: 8px; min-height: 16px; color: #7dd3a0; }
  .msg.err { color: #ff9aa8; }
  details summary { cursor: pointer; font-size: 13px; color: #cfd6e2; font-weight: 600; }
  body.fullscreen .stage { position: fixed; inset: 0; z-index: 50; border-radius: 0; border: 0; }
  body.fullscreen .stage img { height: auto; object-fit: contain; }
  body.fullscreen .stage .hint { background: #000; }
</style>
</head>
<body>
<header>
  <h1>📷 초경량 CCTV</h1>
  <span class="pill"><span class="dot" id="dot"></span><span id="conn">연결 확인 중</span></span>
  <span class="pill">🔋 <span id="battery">-</span></span>
  <span class="pill">🕒 <span id="lastframe">-</span></span>
  <span class="spacer"></span>
  <button class="linkbtn" id="btnGrid" type="button">▦ 한눈에 보기</button>
  <form method="post" action="/cctv/logout" style="margin:0">
    <button class="linkbtn" type="submit">로그아웃</button>
  </form>
</header>

<div class="tabs" id="devTabs"></div>

<main>
  <div class="stage" id="stage">
    <img id="video" alt="카메라 영상">
    <div class="hint" id="hint">
      <b id="hintTitle">카메라 연결을 기다리는 중…</b>
      <span id="hintBody">폰에서 ‘초경량 CCTV’ 앱을 켜고 시작을 누르면 영상이 나타납니다.</span>
    </div>
  </div>

  <div class="gridview" id="grid"></div>

  <div class="single">
    <div class="controls">
      <button id="btnCamera" title="전면/후면 바꾸기">📷 카메라: 후면</button>
      <button id="btnFlash" title="플래시 켜기/끄기">🔦 플래시 꺼짐</button>
      <button id="btnSnap">💾 스냅샷</button>
      <button id="btnFull">⛶ 크게 보기</button>
      <button id="btnRotate" title="누를 때마다 시계 방향으로 90도 회전">🔄 회전 0°</button>
      <button id="btnReload" title="영상을 다시 붙입니다">↻ 다시 연결</button>
    </div>
    <div class="controls">
      <button data-cmd="quality_low" id="btnQLow">화질 낮음</button>
      <button data-cmd="quality_mid" id="btnQMid">화질 중간</button>
      <button data-cmd="quality_high" id="btnQHigh">화질 높음</button>
    </div>

    <div class="row card" style="margin-top:8px">
      <span>🎞 초당 프레임 <span class="val" id="fpsNow">-</span></span>
    </div>
    <div class="controls fpsrow" id="fpsRow">
      <button data-fps="1">1</button>
      <button data-fps="2">2</button>
      <button data-fps="3">3</button>
      <button data-fps="5">5</button>
      <button data-fps="8">8</button>
      <button data-fps="10">10</button>
    </div>

    <div class="row card" style="margin-top:8px">
      <span>기기 <span class="val" id="devname">-</span></span>
      <span>· 화질 <span class="val" id="quality">-</span></span>
      <span>· 카메라 <span class="val" id="camstat">-</span></span>
      <span>· 플래시 <span class="val" id="flash">-</span></span>
      <span>· 화면 <span class="val" id="screen">-</span></span>
      <span>· 앱 <span class="val" id="appv">-</span></span>
      <span>· <span id="streamState">-</span></span>
    </div>

    <div class="card" style="margin-top:8px">
      <details>
        <summary>⚙️ 설정 (비밀번호 변경 · 카메라 토큰)</summary>
        <p style="margin-top:10px">카메라 토큰은 폰 앱의 ‘카메라 토큰’ 칸에 넣는 값입니다. 바꾸면 폰 앱에도 새 값을 넣어야 합니다.</p>
        <div class="row" style="margin-bottom:10px">
          <span>현재 토큰</span>
          <span class="mono val" id="tokenHint">-</span>
        </div>
        <input id="newpw" type="password" placeholder="새 뷰어 비밀번호 (6자 이상)" autocomplete="new-password">
        <input id="newpw2" type="password" placeholder="새 비밀번호 한 번 더" autocomplete="new-password">
        <div class="row">
          <button class="act primary" id="btnSavePw">비밀번호 변경</button>
          <button class="act" id="btnNewToken">카메라 토큰 재발급</button>
        </div>
        <div class="msg" id="setMsg"></div>
      </details>
    </div>
  </div>
</main>

<script>
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var streamImg = $('video'), hint = $('hint');
  var qualityText = { low: '낮음', mid: '중간', high: '높음' };
  var FPS_LIST = [1, 2, 3, 5, 8, 10];

  // ---- 기기 목록·선택 ---------------------------------------------------
  var devices = [];          // /api/cctv/devices 결과
  var selected = '';         // 지금 보고 있는 기기 키
  var gridMode = false;      // 한눈에 보기
  var gridSignature = '';    // 그리드 타일을 다시 만들 필요가 있는지 비교용
  var statusSeq = -1;

  function esc(text) {
    return String(text == null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function saveSelected(key) {
    try { localStorage.setItem('cctv-device', key || ''); } catch (e) {}
  }
  try { selected = localStorage.getItem('cctv-device') || ''; } catch (e) { selected = ''; }

  function deviceByKey(key) {
    for (var i = 0; i < devices.length; i++) {
      if (devices[i].device === key) return devices[i];
    }
    return null;
  }

  function deviceLabel(d) {
    if (!d) return '';
    return d.device_name || d.device || '';
  }

  function streamUrlFor(key) {
    return '/api/cctv/stream' + (key ? ('?device=' + encodeURIComponent(key) + '&t=') : '?t=') + Date.now();
  }

  var tabsSignature = '';

  function renderTabs() {
    var box = $('devTabs');
    // 3초마다 새로 그리면 탭을 누르려는 순간 DOM 이 바뀐다 — 보이는 내용이 그대로면 다시 그리지 않는다.
    var signature = devices.map(function (d) {
      return [d.device, d.online ? 1 : 0, deviceLabel(d), d.fps || 0].join(':');
    }).join('|') + '#' + selected + '#' + (gridMode ? 1 : 0);
    if (signature === tabsSignature && box.childNodes.length) {
      $('btnGrid').style.display = devices.length > 1 ? '' : 'none';
      $('btnGrid').className = 'linkbtn' + (gridMode ? ' on' : '');
      $('btnGrid').textContent = gridMode ? '▣ 한 대만 보기' : '▦ 한눈에 보기';
      return;
    }
    tabsSignature = signature;
    var html = '';
    for (var i = 0; i < devices.length; i++) {
      var d = devices[i];
      var on = (d.device === selected && !gridMode) ? ' on' : '';
      html += '<button type="button" class="tab' + on + '" data-device="' + esc(d.device) + '">'
        + '<span class="dot ' + (d.online ? 'on' : 'off') + '"></span>'
        + '📷 ' + esc(deviceLabel(d))
        + (d.fps ? ' · ' + esc(d.fps) + 'fps' : '')
        + '</button>';
    }
    // 헤더의 '한눈에 보기' 버튼은 기기가 2대 이상일 때만 보여 준다.
    $('btnGrid').style.display = devices.length > 1 ? '' : 'none';
    box.innerHTML = html;
    Array.prototype.forEach.call(box.querySelectorAll('.tab'), function (btn) {
      btn.addEventListener('click', function () {
        var key = btn.getAttribute('data-device');
        if (!key) return;
        setGrid(false);
        selectDevice(key);
      });
    });
    $('btnGrid').className = 'linkbtn' + (gridMode ? ' on' : '');
    $('btnGrid').textContent = gridMode ? '▣ 한 대만 보기' : '▦ 한눈에 보기';
  }

  function selectDevice(key) {
    if (!key || key === selected) { renderTabs(); return; }
    selected = key;
    saveSelected(key);
    statusSeq = -1;
    rot = rotationFor(key);
    var rotateBtn = $('btnRotate');
    if (rotateBtn) rotateBtn.textContent = '🔄 회전 ' + rot + '°';
    renderTabs();
    layoutVideo();
    connectStream(true);
    refresh();
  }

  // ---- 영상 회전: 기기별로 0° → 90° → 180° → 270° (localStorage) --------
  // 예전에는 기기 구분 없이 'cctv-rotation' 하나를 썼다. 첫 기기만 그 값을 물려받고
  // 나머지 기기는 0° 부터 시작하도록 한 번만 옮긴다.
  var legacyRot = null;
  try {
    var legacyValue = parseInt(localStorage.getItem('cctv-rotation'), 10);
    if (!isNaN(legacyValue) && legacyValue % 90 === 0) legacyRot = ((legacyValue % 360) + 360) % 360;
  } catch (e) { legacyRot = null; }

  function rotationFor(key) {
    try {
      var raw = localStorage.getItem('cctv-rotation:' + (key || ''));
      if (raw !== null) {
        var saved = parseInt(raw, 10);
        if (!isNaN(saved) && saved % 90 === 0) return ((saved % 360) + 360) % 360;
      }
    } catch (e) {}
    if (legacyRot !== null) {
      var value = legacyRot;
      legacyRot = null;
      try {
        localStorage.removeItem('cctv-rotation');
        localStorage.setItem('cctv-rotation:' + (key || ''), String(value));
      } catch (e) {}
      return value;
    }
    return 0;
  }

  var rot = rotationFor(selected);

  function setRotation(deg) {
    rot = ((deg % 360) + 360) % 360;
    try {
      localStorage.setItem('cctv-rotation:' + (selected || ''), String(rot));
    } catch (e) {}
    var btn = $('btnRotate');
    if (btn) btn.textContent = '🔄 회전 ' + rot + '°';
    layoutVideo();
    applyGridRotation();
  }

  // ---- 스트림 자동 재연결 -------------------------------------------------
  // 모바일 브라우저는 화면을 벗어나면 MJPEG 연결을 끊어 버린다. 돌아왔을 때
  // 멈춘 화면이 남지 않도록 다시 붙이고, 안 보이는 동안에는 연결을 닫아
  // 데이터·배터리를 아낀다.
  var RECONNECT_COOLDOWN_MS = 1500;   // 너무 자주 새로 붙지 않게
  var STALL_LIMIT_MS = 12000;         // 카메라는 켜졌는데 화면이 이만큼 안 바뀌면 다시 연결
  var lastConnectAt = 0;
  var lastFrameSeq = -1;
  var lastFrameSeqAt = 0;
  var streamPaused = false;
  var wasHidden = false;

  function connectStream(force) {
    if (streamPaused || gridMode) return;
    var now = Date.now();
    if (!force && now - lastConnectAt < RECONNECT_COOLDOWN_MS) return;
    lastConnectAt = now;
    lastFrameSeqAt = now;
    streamImg.src = streamUrlFor(selected);
  }

  function pauseStream() {
    streamPaused = true;
    try { streamImg.removeAttribute('src'); } catch (e) {}
  }

  function resumeStream() {
    streamPaused = false;
    connectStream(true);
    refresh();
  }

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') {
      wasHidden = true;
      pauseStream();
      stopGridStreams();
    } else if (wasHidden || streamPaused) {
      wasHidden = false;
      resumeStream();
      if (gridMode) startGridStreams();
    }
  });

  window.addEventListener('focus', function () {
    if (document.visibilityState !== 'hidden' && (wasHidden || streamPaused)) {
      wasHidden = false;
      resumeStream();
      if (gridMode) startGridStreams();
    }
  });

  // 뒤로 가기로 되살아난 페이지(bfcache)에서는 끊긴 스트림이 그대로 남는다.
  window.addEventListener('pageshow', function (event) {
    if (document.visibilityState === 'hidden') return;
    if (event && event.persisted) {
      streamPaused = false;
      connectStream(true);
    } else {
      connectStream(false);
    }
  });

  // ---- 영상 크기·회전 배치 ------------------------------------------------
  var ratioCache = 0;

  function videoRatio() {
    var w = streamImg.naturalWidth || 0, h = streamImg.naturalHeight || 0;
    if (w > 0 && h > 0) return w / h;
    return 4 / 3;   // 기본 화질(mid) 촬영 비율
  }

  function isFullscreen() { return document.body.classList.contains('fullscreen'); }

  /** 회전 각도에 맞춰 영상 크기와 stage 높이를 다시 잡는다(90°/270° 에서도 안 잘리게). */
  function layoutVideo() {
    var stage = $('stage');
    if (!stage) return;
    var full = isFullscreen();
    var cw = stage.clientWidth || Math.max(240, window.innerWidth - 24);
    var maxH = full
      ? (stage.clientHeight || window.innerHeight)
      : Math.max(200, Math.min(Math.round(window.innerHeight * 0.66), 720));
    var r = videoRatio();
    var hImg, wImg, boxH;
    if (rot % 180 === 0) {
      // 그대로 보일 때: 영상 높이가 stage 높이
      hImg = Math.min(cw / r, maxH);
      wImg = hImg * r;
      boxH = hImg;
    } else {
      // 90°/270°: 회전한 상자의 폭이 영상 높이, 높이가 영상 폭이 된다
      hImg = Math.min(cw, maxH / r) - 1;
      wImg = hImg * r;
      boxH = wImg;
    }
    hImg = Math.max(60, Math.floor(hImg));
    wImg = Math.max(60, Math.floor(wImg));
    boxH = Math.max(60, Math.floor(boxH));
    if (full) {
      stage.style.height = '';
    } else {
      stage.style.height = boxH + 'px';
    }
    streamImg.style.width = wImg + 'px';
    streamImg.style.height = hImg + 'px';
    streamImg.style.transform = 'rotate(' + rot + 'deg)';
  }

  var resizeTimer = null;
  window.addEventListener('resize', function () {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(layoutVideo, 120);
  });

  $('btnRotate').addEventListener('click', function () {
    setRotation(rot + 90);
  });

  streamImg.addEventListener('load', function () {
    lastFrameSeqAt = Date.now();
    var r = videoRatio();
    if (!ratioCache || Math.abs(r - ratioCache) / ratioCache > 0.02) {
      ratioCache = r;
      layoutVideo();
    }
  });
  streamImg.addEventListener('error', function () {
    if (streamPaused || gridMode) return;
    setTimeout(function () { connectStream(true); }, 1200);
  });

  // ---- 한눈에 보기(그리드) ------------------------------------------------
  function setGrid(on) {
    if (gridMode === on) { renderTabs(); return; }
    gridMode = on;
    document.body.classList.toggle('grid', gridMode);
    $('grid').classList.toggle('show', gridMode);
    renderTabs();
    if (gridMode) {
      pauseStream();
      renderGrid(true);
      startGridStreams();
    } else {
      stopGridStreams();
      renderGrid(false);
      streamPaused = false;
      if (!selected) {
        var first = devices[0];
        selected = first ? first.device : '';
        saveSelected(selected);
      }
      connectStream(true);
      layoutVideo();
    }
  }

  function renderGrid(force) {
    if (!gridMode && !force) return;
    var signature = devices.map(function (d) { return d.device; }).join('|');
    if (!force && signature === gridSignature) {
      // 기기 구성이 그대로면 라벨·온라인 표시만 새로 칠한다.
      Array.prototype.forEach.call($('grid').querySelectorAll('.tile'), function (tile) {
        var key = tile.getAttribute('data-device');
        var d = deviceByKey(key);
        var label = tile.querySelector('.tlabel');
        if (label) label.textContent = (d && d.online ? '● ' : '○ ') + deviceLabel(d || { device: key });
      });
      return;
    }
    gridSignature = signature;
    var box = $('grid');
    box.innerHTML = '';
    if (!devices.length) {
      var empty = document.createElement('div');
      empty.className = 'card';
      empty.textContent = '등록된 기기가 없습니다. 폰에서 초경량 CCTV 앱을 켜고 [촬영 시작]을 누르면 여기에 나타납니다.';
      box.appendChild(empty);
      return;
    }
    devices.forEach(function (d) {
      var tile = document.createElement('div');
      tile.className = 'tile';
      tile.setAttribute('data-device', d.device);
      var img = document.createElement('img');
      img.className = 'gimg';
      img.alt = deviceLabel(d);
      img.addEventListener('click', function () {
        setGrid(false);
        selectDevice(d.device);
      });
      img.addEventListener('error', function () {
        if (!gridMode) return;
        setTimeout(function () { if (gridMode) img.src = streamUrlFor(d.device); }, 1500);
      });
      var label = document.createElement('div');
      label.className = 'tlabel';
      label.textContent = (d.online ? '● ' : '○ ') + deviceLabel(d);
      tile.appendChild(img);
      tile.appendChild(label);
      box.appendChild(tile);
    });
    applyGridRotation();
  }

  function applyGridRotation() {
    Array.prototype.forEach.call($('grid').querySelectorAll('.tile'), function (tile) {
      var key = tile.getAttribute('data-device');
      var img = tile.querySelector('img');
      if (!img) return;
      var deg = rotationFor(key);
      img.style.transform = deg ? ('rotate(' + deg + 'deg)') : '';
    });
  }

  function startGridStreams() {
    Array.prototype.forEach.call($('grid').querySelectorAll('img'), function (img) {
      img.src = streamUrlFor(img.parentNode.getAttribute('data-device'));
    });
  }

  function stopGridStreams() {
    Array.prototype.forEach.call($('grid').querySelectorAll('img'), function (img) {
      try { img.removeAttribute('src'); } catch (e) {}
    });
  }

  $('btnGrid').addEventListener('click', function () { setGrid(!gridMode); });
  $('btnReload').addEventListener('click', function () {
    if (gridMode) { stopGridStreams(); startGridStreams(); }
    else connectStream(true);
    refresh();
  });

  // ---- 서버 호출 ----------------------------------------------------------
  function post(path, data) {
    return fetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data || {})
    }).then(function (r) {
      if (r.status === 401) { location.href = '/cctv'; throw new Error('로그인이 필요합니다.'); }
      return r.json().then(function (j) { if (!r.ok) throw new Error(j.detail || '요청 실패'); return j; });
    });
  }

  function command(name, extra) {
    var body = { command: name, device: selected };
    if (extra) {
      for (var k in extra) { if (Object.prototype.hasOwnProperty.call(extra, k)) body[k] = extra[k]; }
    }
    return post('/api/cctv/command', body);
  }

  function fmtAgo(sec) {
    if (sec === null || sec === undefined) return '-';
    if (sec < 3) return '방금';
    if (sec < 60) return sec + '초 전';
    if (sec < 3600) return Math.floor(sec / 60) + '분 전';
    return Math.floor(sec / 3600) + '시간 전';
  }

  function refreshDevices(cb) {
    fetch('/api/cctv/devices', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { if (r.status === 401) location.href = '/cctv'; return r.json(); })
      .then(function (j) {
        devices = (j && j.devices) || [];
        var found = deviceByKey(selected);
        if (!found) {
          var online = null;
          for (var i = 0; i < devices.length; i++) { if (devices[i].online) { online = devices[i]; break; } }
          var pick = online || devices[0];
          selected = pick ? pick.device : '';
          saveSelected(selected);
          rot = rotationFor(selected);
          if ($('btnRotate')) $('btnRotate').textContent = '🔄 회전 ' + rot + '°';
          statusSeq = -1;
        }
        renderTabs();
        if (gridMode) renderGrid(false);
        if (cb) cb();
      })
      .catch(function () {});
  }

  var flashOn = false;

  function applyStatus(s) {
    var online = !!s.camera_online;
    $('dot').className = 'dot ' + (online ? 'on' : 'off');
    $('conn').textContent = online ? '카메라 연결됨' : '카메라 오프라인';
    $('battery').textContent = (s.battery === null || s.battery === undefined)
      ? '-' : (s.battery + '%' + (s.charging ? ' ⚡' : ''));
    $('lastframe').textContent = '영상 ' + fmtAgo(s.last_frame_sec_ago);
    $('devname').textContent = s.device_name || '-';
    $('quality').textContent = qualityText[s.quality] || s.quality || '-';
    $('camstat').textContent = s.camera === 'front' ? '전면' : (s.camera === 'back' ? '후면' : '-');
    $('flash').textContent = s.flash ? '켜짐' : '꺼짐';
    flashOn = !!s.flash;
    $('screen').textContent = s.screen_on === false ? '꺼짐' : (s.screen_on ? '켜짐' : '-');
    $('appv').textContent = s.app_version || '-';
    $('tokenHint').textContent = s.camera_token_hint || '-';
    $('fpsNow').textContent = (s.fps === null || s.fps === undefined) ? '-' : (s.fps + ' fps');
    $('btnQLow').className = s.quality === 'low' ? 'on' : '';
    $('btnQMid').className = s.quality === 'mid' ? 'on' : '';
    $('btnQHigh').className = s.quality === 'high' ? 'on' : '';
    $('btnCamera').textContent = s.camera === 'front' ? '🤳 카메라: 전면' : '📷 카메라: 후면';
    // 전면 카메라에는 플래시가 없다 — 버튼을 잠근다(불필요한 명령을 막는다).
    var front = s.camera === 'front';
    $('btnFlash').disabled = front;
    if (front) {
      $('btnFlash').textContent = '🔦 플래시(후면 전용)';
      $('btnFlash').className = '';
    } else {
      $('btnFlash').textContent = s.flash ? '💡 플래시 켜짐' : '🔦 플래시 꺼짐';
      $('btnFlash').className = s.flash ? 'on' : '';
    }
    $('streamState').textContent = s.streaming_wanted ? '실시간 전송 중' : '대기(전송 안 함)';
    Array.prototype.forEach.call($('fpsRow').querySelectorAll('button'), function (btn) {
      var value = parseInt(btn.getAttribute('data-fps'), 10);
      btn.className = (s.fps !== null && s.fps !== undefined && value === s.fps) ? 'on' : '';
    });

    var fresh = s.last_frame_sec_ago !== null && s.last_frame_sec_ago !== undefined && s.last_frame_sec_ago <= 8;
    // 상태는 도는데 이 화면의 프레임만 안 바뀌면 스트림을 다시 붙인다.
    var seq = (s.frame_seq === undefined || s.frame_seq === null) ? -1 : s.frame_seq;
    var nowMs = Date.now();
    if (seq !== lastFrameSeq) {
      lastFrameSeq = seq;
      lastFrameSeqAt = nowMs;
    } else if (s.camera_online && !streamPaused && !gridMode && lastFrameSeqAt && nowMs - lastFrameSeqAt > STALL_LIMIT_MS) {
      connectStream(false);
    }
    if (fresh) {
      hint.style.display = 'none';
    } else {
      hint.style.display = 'flex';
      if (online) {
        $('hintTitle').textContent = '영상을 기다리는 중…';
        $('hintBody').textContent = '카메라는 연결돼 있는데 아직 새 영상이 없습니다. 잠시만 기다려 주세요.';
      } else {
        $('hintTitle').textContent = '카메라가 꺼져 있습니다';
        $('hintBody').textContent = '폰에서 초경량 CCTV 앱을 켜고 [촬영 시작]을 눌러 주세요.';
      }
    }
  }

  function refresh() {
    var url = '/api/cctv/status' + (selected ? ('?device=' + encodeURIComponent(selected)) : '');
    return fetch(url, { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { if (r.status === 401) location.href = '/cctv'; return r.json(); })
      .then(function (s) {
        if (!selected && s && s.device) {
          selected = s.device;
          saveSelected(selected);
          rot = rotationFor(selected);
          renderTabs();
          connectStream(true);
        }
        applyStatus(s || {});
      })
      .catch(function () {});
  }

  Array.prototype.forEach.call(document.querySelectorAll('[data-cmd]'), function (btn) {
    btn.addEventListener('click', function () {
      var cmd = btn.getAttribute('data-cmd');
      command(cmd).catch(function (e) { alert(e.message); });
      setTimeout(refresh, 600);
    });
  });

  Array.prototype.forEach.call($('fpsRow').querySelectorAll('button'), function (btn) {
    btn.addEventListener('click', function () {
      var fps = parseInt(btn.getAttribute('data-fps'), 10);
      command('fps', { fps: fps })
        .then(function () { $('fpsNow').textContent = fps + ' fps'; setTimeout(refresh, 700); })
        .catch(function (e) { alert(e.message); });
    });
  });

  // 카메라도 한 버튼 토글로: 지금 상태의 반대(전면 ↔ 후면)를 보낸다.
  $('btnCamera').addEventListener('click', function () {
    var next = ($('camstat').textContent === '전면') ? 'back' : 'front';
    command(next)
      .then(function () { setTimeout(refresh, 500); })
      .catch(function (e) { alert(e.message); });
  });

  // 플래시 켜기/끄기는 한 버튼으로: 지금 상태의 반대를 보낸다.
  $('btnFlash').addEventListener('click', function () {
    if ($('camstat').textContent === '전면') {
      return;   // 전면 카메라에서는 플래시를 쓰지 않는다
    }
    var next = !flashOn;
    flashOn = next;
    $('btnFlash').textContent = next ? '💡 플래시 켜짐' : '🔦 플래시 꺼짐';
    $('btnFlash').className = next ? 'on' : '';
    command(next ? 'flash_on' : 'flash_off')
      .then(function () { setTimeout(refresh, 600); })
      .catch(function (e) { alert(e.message); refresh(); });
  });

  /** 지금 고른 회전 각도대로 캔버스에서 돌려 JPEG 로 만든다(0° 는 원본 그대로). */
  function rotateBlob(blob, deg) {
    if (deg % 360 === 0) return Promise.resolve(blob);
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(blob);
      var img = new Image();
      img.onload = function () {
        try {
          var w = img.naturalWidth || img.width;
          var h = img.naturalHeight || img.height;
          var swap = deg % 180 !== 0;
          var canvas = document.createElement('canvas');
          canvas.width = swap ? h : w;
          canvas.height = swap ? w : h;
          var ctx = canvas.getContext('2d');
          ctx.translate(canvas.width / 2, canvas.height / 2);
          ctx.rotate(deg * Math.PI / 180);
          ctx.drawImage(img, -w / 2, -h / 2, w, h);
          canvas.toBlob(function (out) {
            URL.revokeObjectURL(url);
            if (out) resolve(out); else reject(new Error('이미지를 만들지 못했습니다.'));
          }, 'image/jpeg', 0.92);
        } catch (e) {
          URL.revokeObjectURL(url);
          reject(e);
        }
      };
      img.onerror = function () {
        URL.revokeObjectURL(url);
        reject(new Error('영상을 읽지 못했습니다.'));
      };
      img.src = url;
    });
  }

  $('btnSnap').addEventListener('click', function () {
    var deg = rot;
    var url = '/api/cctv/frame' + (selected ? ('?device=' + encodeURIComponent(selected)) : '');
    fetch(url, { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('아직 저장할 영상이 없습니다.');
        return r.blob();
      })
      .then(function (blob) { return rotateBlob(blob, deg); })
      .then(function (blob) {
        var d = new Date();
        var p = function (n) { return (n < 10 ? '0' : '') + n; };
        var name = 'cctv-' + (selected ? selected + '-' : '') + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '-'
          + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds()) + '.jpg';
        var a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = name;
        document.body.appendChild(a);
        a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
      })
      .catch(function (e) { alert(e.message); });
  });

  function toggleFull() {
    document.body.classList.toggle('fullscreen');
    $('btnFull').textContent = isFullscreen() ? '✕ 닫기' : '⛶ 크게 보기';
    // 전체 화면으로 바뀐 뒤 화면 크기에 맞춰 다시 배치한다.
    requestAnimationFrame(function () { layoutVideo(); });
    setTimeout(layoutVideo, 60);
  }

  $('btnFull').addEventListener('click', function () {
    toggleFull();
  });

  $('stage').addEventListener('click', function () {
    if (isFullscreen()) toggleFull();
  });

  function setMsg(text, isErr) {
    var m = $('setMsg');
    m.textContent = text;
    m.className = 'msg' + (isErr ? ' err' : '');
  }

  $('btnSavePw').addEventListener('click', function () {
    var a = $('newpw').value, b = $('newpw2').value;
    if (a.length < 6) { setMsg('비밀번호는 6자 이상이어야 합니다.', true); return; }
    if (a !== b) { setMsg('두 비밀번호가 다릅니다.', true); return; }
    post('/api/cctv/settings', { new_password: a })
      .then(function () {
        setMsg('비밀번호를 바꿨습니다. 다음 로그인부터 새 비밀번호를 쓰세요.');
        $('newpw').value = '';
        $('newpw2').value = '';
      })
      .catch(function (e) { setMsg(e.message, true); });
  });

  $('btnNewToken').addEventListener('click', function () {
    if (!confirm('카메라 토큰을 새로 만들까요? 폰 앱에도 새 토큰을 넣어야 영상이 올라옵니다.')) return;
    post('/api/cctv/settings', { regenerate_camera_token: true })
      .then(function (j) {
        setMsg('새 카메라 토큰: ' + j.camera_token + '  (폰 앱에 입력하세요)');
        refresh();
      })
      .catch(function (e) { setMsg(e.message, true); });
  });

  $('btnRotate').textContent = '🔄 회전 ' + rot + '°';
  layoutVideo();
  refreshDevices(function () {
    refresh();
    if (gridMode) { renderGrid(true); startGridStreams(); }
    else connectStream(true);
  });
  setInterval(refresh, 3000);
  setInterval(function () { refreshDevices(function () { refresh(); }); }, 9000);
})();
</script>
</body>
</html>
"""


def render_login(error: str = "") -> str:
    block = '<div class="err">' + error + '</div>' if error else ""
    return LOGIN_HTML.replace("__ERROR__", block)


def render_viewer() -> str:
    return VIEWER_HTML

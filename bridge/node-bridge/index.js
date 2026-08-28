/**
 * node-bridge — Playwright-based Zoom bridge for Mac.
 * Replaces the C++ Linux SDK bridge.
 * Same HTTP/SSE interface on port 8765 so Python orchestrator is unchanged.
 *
 * Reconnect theme:
 *  1) Detect state (preview Join page / waiting / in-meeting / sign-in)
 *  2) Auto-click Join when dropped back to preview
 *  3) Full re-navigate with stored panelist tk if the page is gone
 */

const { chromium } = require('playwright');
const express = require('express');

process.on('unhandledRejection', (err) => {
  console.error('[bridge] unhandledRejection:', err && err.message || err);
});
process.on('uncaughtException', (err) => {
  console.error('[bridge] uncaughtException:', err && err.message || err);
});

const app = express();
app.use(express.json());

let browser = null;
let page = null;
const sseClients = [];
let joining = false;
let reconnecting = false;
let lastJoin = null; // {meeting_id, password, display_name, webinar_token, join_url}
let meetingState = 'idle'; // idle|joining|preview|waiting|in_meeting|signin|disconnected
let lastJoinError = null;
let joinStartedAt = null;
let lastJoinFinishedAt = null;
let watchdog = null;
// "unknown" is the fallback for a page that hasn't finished loading yet (a fresh
// navigation is briefly blank), not just for genuine failures — require it to
// persist across a few watchdog ticks before treating it as broken.
let unknownStreak = 0;
const UNKNOWN_STREAK_THRESHOLD = 3;

function pastBridgeEnd() {
  const raw = process.env.BRIDGE_END_AT || '';
  if (!raw) return false;
  const end = Date.parse(raw.includes('T') ? raw : raw.replace(' ', 'T'));
  return Number.isFinite(end) && Date.now() >= end;
}

// ---- SSE helpers --------------------------------------------------------

function emit(obj) {
  const line = `data: ${JSON.stringify(obj)}\n\n`;
  sseClients.forEach((res) => res.write(line));
  console.log('[bridge] emit:', JSON.stringify(obj));
}

app.get('/events', (req, res) => {
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.flushHeaders();
  sseClients.push(res);
  const hb = setInterval(() => res.write(': keepalive\n\n'), 15000);
  req.on('close', () => {
    clearInterval(hb);
    const i = sseClients.indexOf(res);
    if (i !== -1) sseClients.splice(i, 1);
  });
});

// ---- Health -------------------------------------------------------------

app.get('/health', (req, res) => {
  const live = meetingState === 'in_meeting' || meetingState === 'waiting';
  res.json({
    status: 'ok',
    in_meeting: live,
    meeting_state: meetingState,
    has_page: !!(page && !page.isClosed()),
    reconnecting,
    joining,
    last_join_error: lastJoinError,
    join_started_at: joinStartedAt,
    last_join_finished_at: lastJoinFinishedAt,
  });
});

// ---- Meeting state detection --------------------------------------------

async function detectMeetingState(pg) {
  if (!pg || pg.isClosed()) return 'disconnected';
  try {
    return await pg.evaluate(() => {
      const text = (document.body && document.body.innerText) || '';
      const href = location.href || '';
      if (/sign.?in|login/i.test(href)) return 'signin';
      if (/Waiting for host/i.test(text)) return 'waiting';
      const joinBtn = [...document.querySelectorAll('button')].find((b) => {
        const t = (b.innerText || '').trim();
        const c = String(b.className || '');
        return t === 'Join' || c.includes('preview-join-button');
      });
      const chat =
        document.querySelector('button[aria-label*="chat" i]') ||
        document.querySelector('textarea.chat-box__chat-textarea');
      // Preview Join page after disconnect
      if (joinBtn && !chat) return 'preview';
      if (chat) return 'in_meeting';
      if (/you (have )?left|removed from|meeting has ended|disconnected/i.test(text)) {
        return 'disconnected';
      }
      return 'unknown';
    });
  } catch {
    return 'disconnected';
  }
}

async function clickPreviewJoin(pg) {
  const selectors = [
    'button.preview-join-button',
    'button.zm-btn.preview-join-button',
  ];
  for (const sel of selectors) {
    try {
      const el = await pg.$(sel);
      if (el) {
        await el.click();
        console.log('[bridge] clicked preview Join via', sel);
        return true;
      }
    } catch {}
  }
  // Fallback: any button whose text is exactly Join
  try {
    const clicked = await pg.evaluate(() => {
      const b = [...document.querySelectorAll('button')].find(
        (x) => (x.innerText || '').trim() === 'Join'
      );
      if (!b) return false;
      b.click();
      return true;
    });
    if (clicked) {
      console.log('[bridge] clicked preview Join via text match');
      return true;
    }
  } catch {}
  return false;
}

async function fillNameAndJoin(pg, displayName, password = '') {
  try {
    const nameSel =
      'input[placeholder*="name" i], input[placeholder*="Name" i], #inputname';
    const name = await pg.$(nameSel);
    if (name) {
      await name.fill(displayName);
    }
  } catch {}
  if (password) {
    try {
      const pwdSel =
        '#input-for-pwd, input[placeholder*="passcode" i], input[placeholder*="password" i], input[type="password"]';
      const pwd = await pg.$(pwdSel);
      if (pwd) {
        await pwd.fill(password);
        console.log('[bridge] filled meeting passcode');
      }
    } catch (e) {
      console.log('[bridge] passcode fill skip:', e.message);
    }
  }
  return clickPreviewJoin(pg);
}

// ---- Open chat panel ----------------------------------------------------

async function openChatPanel(pg) {
  const inputSel =
    'textarea.chat-box__chat-textarea, textarea[class*="chat-box__chat-textarea"], textarea[title="chat message"], textarea[placeholder*="Type message" i]';

  const alreadyOpen = await pg.$(inputSel);
  if (alreadyOpen) {
    console.log('[bridge] chat panel already open');
    return true;
  }
  const closeBtn = await pg.$('button[aria-label*="close the chat" i]');
  if (closeBtn) {
    try {
      await pg.waitForSelector(inputSel, { timeout: 5000, state: 'visible' });
      console.log('[bridge] chat panel already open (waited for input)');
      return true;
    } catch {}
  }

  const chatSelectors = [
    'button[aria-label*="open the chat panel" i]',
    'button[aria-label*="open the chat pane" i]',
    '.footer-chat-button > button.footer-button__button[aria-label*="chat" i]',
    'button[aria-label="Chat"]',
  ];
  for (const sel of chatSelectors) {
    try {
      const el = await pg.$(sel);
      if (!el) continue;
      const aria = ((await el.getAttribute('aria-label')) || '').toLowerCase();
      if (aria.includes('close') || aria.includes('settings')) continue;
      await el.click();
      console.log('[bridge] opened chat panel via:', sel);
      await pg.waitForSelector(inputSel, { timeout: 8000, state: 'visible' });
      return true;
    } catch (e) {
      console.log('[bridge] open chat attempt failed:', sel, e.message);
    }
  }
  console.log('[bridge] could not find chat button — will rely on tip monitor');
  return false;
}

// ---- Inject monitors into page ------------------------------------------

async function injectMonitors(pg) {
  try {
    await pg.exposeFunction('__bridgeOnChat', (sender, senderId, text) => {
      if (!text || !text.trim()) return;
      const t = text.trim();
      const cleanSender = (sender || '')
        .replace(/\s+to\s+.*$/i, '')
        .trim();
      if (cleanSender && t === cleanSender) return;
      if (/^\d{1,2}:\d{2}\s*(AM|PM)$/i.test(t)) return;
      if (t.length < 3) return;
      console.log(`[bridge] chat from ${cleanSender || sender}: ${t}`);
      emit({
        type: 'chat_message',
        sender_name: cleanSender || sender,
        sender_id: cleanSender || senderId || sender,
        text: t,
        is_private: false,
      });
    });
  } catch (e) {
    console.log('[bridge] __bridgeOnChat already exposed or error:', e.message);
  }

  pg.on('console', (msg) => {
    const t = msg.text();
    if (t.startsWith('[ZOOM-DOM]') || t.startsWith('[bridge]')) {
      console.log('[bridge-page]', t);
    }
  });

  await pg.evaluate(() => {
    setTimeout(() => {
      const els = document.querySelectorAll(
        '[class*="chat"], [role="log"], [role="list"], [aria-label*="chat" i], .last-chat-message-tip__container'
      );
      els.forEach((el, i) => {
        if (i < 15) {
          console.log(
            `[ZOOM-DOM] ${i} ${el.tagName}.${[...el.classList].join('.')} ` +
              `text="${(el.innerText || '').slice(0, 80).replace(/\n/g, ' ')}"`
          );
        }
      });
    }, 3000);

    {
      const seen = new Set();
      function checkTip() {
        const tip = document.querySelector(
          '.last-chat-message-tip__container, [class*="last-chat-message-tip"]'
        );
        if (!tip) return;
        const fromEl = tip.querySelector(
          '.last-chat-message-tip__from-to, [class*="from-to"], [class*="sender"]'
        );
        const contentEl = tip.querySelector(
          '.last-chat-message-tip__content, [class*="tip__content"], [class*="message-content"]'
        );
        if (!fromEl || !contentEl) {
          const full = tip.innerText || '';
          const lines = full.split('\n').map((s) => s.trim()).filter(Boolean);
          if (lines.length >= 2) {
            const sender = lines[0]
              .replace(/^from:\s*/i, '')
              .replace(/\s*to:.*/i, '')
              .trim();
            const text = lines.slice(1).join(' ').trim();
            const key = `tip:${sender}:${text}`;
            if (!seen.has(key) && text.length >= 3 && text !== sender) {
              seen.add(key);
              setTimeout(() => seen.delete(key), 30000);
              window.__bridgeOnChat(sender, sender, text);
            }
          }
          return;
        }
        const sender = fromEl.innerText
          .replace(/^from:\s*/i, '')
          .replace(/\s*to:.*/i, '')
          .trim();
        const text = contentEl.innerText.trim();
        const key = `tip:${sender}:${text}`;
        if (!seen.has(key) && text.length >= 3 && text !== sender) {
          seen.add(key);
          setTimeout(() => seen.delete(key), 30000);
          window.__bridgeOnChat(sender, sender, text);
        }
      }
      new MutationObserver(checkTip).observe(document.body, {
        childList: true,
        subtree: true,
        characterData: true,
      });
      setInterval(checkTip, 500);
      console.log('[bridge] tip monitor active');
    }

    {
      const seen2 = new Set();
      function getMessages() {
        const strategies = [
          () => document.querySelectorAll('[class*="chat-message--"]'),
          () => document.querySelectorAll('[class*="chatMessage"]'),
          () => document.querySelectorAll('[class*="chat-item"]'),
          () => document.querySelectorAll('[class*="chat-record"]'),
          () => {
            const c = document.querySelector(
              '[class*="chat-list"], [class*="chatList"], [class*="chat-container"], [class*="chat-panel"]'
            );
            return c ? c.querySelectorAll('li, [class*="message"]') : [];
          },
        ];
        for (const fn of strategies) {
          const els = fn();
          if (els && els.length > 0) return els;
        }
        return [];
      }
      function extractMessage(el) {
        const senderEl = el.querySelector(
          '[class*="sender"], [class*="name"], [class*="author"], [class*="display-name"]'
        );
        const textEl = el.querySelector(
          '[class*="text"], [class*="body"], [class*="content"], [class*="message-body"], p'
        );
        if (!senderEl || !textEl) {
          const full = el.innerText || '';
          const colon = full.indexOf(':');
          if (colon > 0 && colon < 50) {
            return {
              sender: full.slice(0, colon).trim(),
              text: full.slice(colon + 1).trim(),
            };
          }
          return null;
        }
        return {
          sender: senderEl.innerText.trim().replace(/:$/, ''),
          text: textEl.innerText.trim(),
        };
      }
      function scanPanel() {
        const msgs = getMessages();
        msgs.forEach((el) => {
          const key = el.innerText;
          if (!key || seen2.has(key)) return;
          seen2.add(key);
          const parsed = extractMessage(el);
          if (
            parsed &&
            parsed.sender &&
            parsed.text &&
            parsed.text.length >= 3 &&
            parsed.text !== parsed.sender
          ) {
            window.__bridgeOnChat(parsed.sender, parsed.sender, parsed.text);
          }
        });
      }
      setInterval(scanPanel, 500);
      new MutationObserver(scanPanel).observe(document.body, {
        childList: true,
        subtree: true,
      });
      console.log('[bridge] panel monitor active');
    }
  });
}

async function postJoinSetup(pg) {
  try {
    await pg.bringToFront();
  } catch {}
  try {
    await pg.evaluate(() => {
      window.focus();
      document.title = document.title || 'Zoom';
    });
  } catch {}
  await openChatPanel(pg);
  await injectMonitors(pg);
  meetingState = await detectMeetingState(pg);
  console.log('[bridge] joined and monitoring chat; state=', meetingState);
  try {
    const chatPerm = await ensurePanelistsChatWithEveryone(pg);
    console.log('[bridge] chat permission result:', JSON.stringify(chatPerm));
    emit({ type: 'chat_settings', ...chatPerm });
  } catch (e) {
    console.log('[bridge] chat settings skip:', e.message);
    emit({ type: 'chat_settings', ok: false, error: e.message });
  }
  startWatchdog();
}

/**
 * Host/co-host control: Chat Settings (footer) or Chat → ⋯ →
 * Panelists can chat with → Everyone
 * So panelist messages reach all attendees (not only hosts/panelists).
 * Requires host or co-host privileges — plain panelists usually cannot change this.
 * @see https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0067761
 */
async function ensurePanelistsChatWithEveryone(pg) {
  if (!pg || pg.isClosed()) {
    return { ok: false, error: 'no page' };
  }
  await openChatPanel(pg);
  await pg.waitForTimeout(600);

  // Prefer the dedicated footer "Chat Settings" control (next to Chat)
  let opened = false;
  try {
    const chatSettings = pg.locator(
      'button[aria-label="Chat Settings"], button[aria-label*="Chat Settings" i]'
    );
    if (await chatSettings.first().isVisible({ timeout: 2000 }).catch(() => false)) {
      await chatSettings.first().click({ timeout: 3000 });
      opened = true;
      console.log('[bridge] opened Chat Settings (footer)');
      await pg.waitForTimeout(700);
    }
  } catch (e) {
    console.log('[bridge] Chat Settings button skip:', e.message);
  }

  // Open the chat "more" / settings menu (⋯)
  const moreSelectors = [
    'button[aria-label*="More" i]',
    'button[aria-label*="more options" i]',
    'button[aria-label*="Chat settings" i]',
    'button[aria-label*="chat settings" i]',
    '.chat-header button[aria-label*="More" i]',
    '[class*="chat"] button[aria-label*="More" i]',
    '[class*="chat-header"] button:has(svg)',
  ];
  if (!opened) {
    for (const sel of moreSelectors) {
      try {
        const btns = await pg.$$(sel);
        for (const b of btns) {
          const box = await b.boundingBox().catch(() => null);
          if (!box) continue;
          const aria = ((await b.getAttribute('aria-label')) || '').toLowerCase();
          if (aria.includes('participant') || aria.includes('reaction')) continue;
          if (
            aria.includes('more') ||
            aria.includes('setting') ||
            aria.includes('option') ||
            !aria
          ) {
            await b.click({ timeout: 2000 });
            opened = true;
            console.log('[bridge] opened chat more via', sel, aria || '(no aria)');
            await pg.waitForTimeout(700);
            break;
          }
        }
        if (opened) break;
      } catch {}
    }
  }

  // Fallback: click any visible ⋯ in chat region via evaluate
  if (!opened) {
    opened = await pg.evaluate(() => {
      const candidates = [...document.querySelectorAll('button, [role="button"]')];
      const hit = candidates.find((el) => {
        const t = (el.getAttribute('aria-label') || el.innerText || '')
          .trim()
          .toLowerCase();
        const cls = String(el.className || '').toLowerCase();
        if (
          !(
            t.includes('more') ||
            t.includes('chat settings') ||
            t === '…' ||
            t === '...' ||
            cls.includes('more')
          )
        ) {
          return false;
        }
        const inChat =
          el.closest('[class*="chat"]') ||
          el.closest('[aria-label*="chat" i]') ||
          el.closest('[class*="Chat"]');
        return !!inChat || t.includes('chat settings');
      });
      if (hit) {
        hit.click();
        return true;
      }
      return false;
    });
    if (opened) {
      console.log('[bridge] opened chat more via evaluate fallback');
      await pg.waitForTimeout(700);
    }
  }

  if (!opened) {
    return {
      ok: false,
      error:
        'Could not open Chat Settings / ⋯ menu. Bot may not be host/co-host, or Zoom UI changed.',
    };
  }

  // Prefer selecting "Everyone" under Panelists can chat with
  const result = await pg.evaluate(() => {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();

    const nodes = [
      ...document.querySelectorAll(
        'button, a, [role="menuitem"], [role="option"], label, div, span, li'
      ),
    ];

    const panelistLabel = nodes.find((n) => {
      const t = norm(n.innerText);
      return (
        t.includes('panelists can chat with') ||
        t.includes('panelist can chat with') ||
        t === 'panelists can chat with'
      );
    });

    const everyoneCandidates = nodes.filter((n) => {
      const t = norm(n.innerText);
      return (
        t === 'everyone' ||
        t === 'everyone including attendees' ||
        t === 'everyone (including attendees)'
      );
    });

    function clickable(n) {
      return (
        n.closest(
          'button, a, [role="menuitem"], [role="option"], label, [role="radio"]'
        ) || n
      );
    }

    if (panelistLabel && everyoneCandidates.length) {
      let best = everyoneCandidates[0];
      let bestDist = Infinity;
      for (const c of everyoneCandidates) {
        const after = !!(
          c.compareDocumentPosition(panelistLabel) &
          Node.DOCUMENT_POSITION_PRECEDING
        );
        const score = after ? 0 : 10;
        if (score < bestDist) {
          bestDist = score;
          best = c;
        }
      }
      clickable(best).click();
      return {
        clicked: true,
        via: 'near_panelists_label',
        text: norm(best.innerText),
      };
    }

    // Sometimes the control is a closed dropdown — open it first
    if (panelistLabel) {
      const trigger =
        panelistLabel.closest('button, [role="button"]') ||
        panelistLabel.parentElement?.querySelector('button, [role="combobox"]');
      if (trigger) trigger.click();
    }

    for (const c of everyoneCandidates) {
      const el = clickable(c);
      const visible = el.offsetParent !== null || el.getClientRects().length > 0;
      if (!visible) continue;
      el.click();
      return { clicked: true, via: 'everyone_option', text: norm(c.innerText) };
    }

    const menuText = nodes
      .map((n) => norm(n.innerText))
      .filter(
        (t) =>
          t &&
          t.length < 80 &&
          (t.includes('chat') || t.includes('everyone') || t.includes('panelist'))
      )
      .slice(0, 20);

    const panelistOnlyUi =
      menuText.some((t) => t.includes('hosts and panelists')) &&
      !menuText.some((t) => t.includes('panelists can chat'));

    return {
      clicked: false,
      menuSample: menuText,
      panelistOnlyUi,
    };
  });

  await pg.waitForTimeout(500);

  // If we opened a dropdown, try Everyone again
  if (result && !result.clicked) {
    const second = await pg.evaluate(() => {
      const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
      const nodes = [
        ...document.querySelectorAll(
          'button, a, [role="menuitem"], [role="option"], label, li'
        ),
      ];
      for (const n of nodes) {
        const t = norm(n.innerText);
        if (
          t === 'everyone' ||
          t === 'everyone including attendees' ||
          t === 'everyone (including attendees)'
        ) {
          (n.closest('button, a, [role="menuitem"], [role="option"], label') || n).click();
          return { clicked: true, text: t };
        }
      }
      return { clicked: false };
    });
    await pg.waitForTimeout(400);
    if (second && second.clicked) {
      return { ok: true, mode: 'everyone', detail: second };
    }
  }

  if (result && result.clicked) {
    return { ok: true, mode: 'everyone', detail: result };
  }

  if (result && result.panelistOnlyUi) {
    return {
      ok: false,
      needs_cohost: true,
      error:
        'Joined as panelist only — Zoom hides "Panelists can chat with → Everyone" ' +
        'from panelists. Make Hermes co-host (or set it from the host account), then retry POST /chat/settings/panelists_everyone.',
      detail: result,
    };
  }

  return {
    ok: false,
    error:
      'Chat settings opened but could not select Panelists → Everyone. ' +
      'Make Hermes co-host/host, or set it manually. ' +
      `Seen: ${(result && result.menuSample && result.menuSample.join(' | ')) || 'none'}`,
    detail: result,
  };
}

// ---- Watchdog / reconnect -----------------------------------------------

function startWatchdog() {
  if (watchdog) return;
  watchdog = setInterval(() => {
    recoverIfNeeded().catch((e) =>
      console.error('[bridge] watchdog error:', e.message)
    );
  }, 4000);
  console.log('[bridge] reconnect watchdog started');
}

function stopWatchdog() {
  if (watchdog) {
    clearInterval(watchdog);
    watchdog = null;
  }
}

async function recoverIfNeeded() {
  if (pastBridgeEnd()) {
    console.log('[bridge] end time reached — reconnect disabled');
    meetingState = 'disconnected';
    stopWatchdog();
    return;
  }
  // A failed navigation can close the page while doJoin is still waiting for
  // Zoom. Do not let that transient flag permanently disable recovery.
  if (joining) {
    const elapsed = joinStartedAt ? Date.now() - joinStartedAt : 0;
    const pageGone = !page || page.isClosed();
    if (!pageGone && elapsed < 45000) return;
    console.log('[bridge] join watchdog recovery:', pageGone ? 'page_gone' : 'join_timeout');
    joining = false;
    if (!lastJoinError) lastJoinError = pageGone ? 'Zoom page closed during join' : 'Zoom join timed out';
  }
  if (reconnecting) return;
  if (!lastJoin) return;

  const prev = meetingState;
  const state = await detectMeetingState(page);
  meetingState = state;

  if (state !== 'unknown') {
    unknownStreak = 0;
  } else {
    unknownStreak += 1;
    if (unknownStreak < UNKNOWN_STREAK_THRESHOLD) {
      console.log(
        `[bridge] unknown state (${unknownStreak}/${UNKNOWN_STREAK_THRESHOLD}) — page may still be loading, holding off`
      );
      return;
    }
  }

  if (state === 'in_meeting' || state === 'waiting') {
    if (prev !== state && (prev === 'preview' || prev === 'disconnected' || prev === 'unknown')) {
      emit({ type: 'reconnected', meeting_state: state });
      try {
        await postJoinSetup(page);
      } catch (e) {
        console.log('[bridge] post-reconnect setup:', e.message);
      }
    } else if (prev === 'waiting' && state === 'in_meeting') {
      // Chat permission menus often only appear after the webinar goes live
      try {
        const chatPerm = await ensurePanelistsChatWithEveryone(page);
        console.log('[bridge] chat permission (live):', JSON.stringify(chatPerm));
        emit({ type: 'chat_settings', ...chatPerm });
      } catch (e) {
        console.log('[bridge] chat settings on live:', e.message);
      }
    }
    return;
  }

  // Dropped to Join preview — click Join first (fast path)
  if (state === 'preview') {
    console.log('[bridge] detected preview Join page — reconnecting (click Join)');
    emit({ type: 'disconnected', reason: 'preview', meeting_state: state });
    reconnecting = true;
    emit({ type: 'reconnecting', strategy: 'click_join' });
    try {
      const ok = await clickPreviewJoin(page);
      if (ok) {
        await page.waitForTimeout(5000);
        meetingState = await detectMeetingState(page);
        if (meetingState === 'in_meeting' || meetingState === 'waiting') {
          await postJoinSetup(page);
          emit({ type: 'reconnected', meeting_state: meetingState });
          return;
        }
      }
      // Fall through to full rejoin
      console.log('[bridge] Join click insufficient — full rejoin');
      await fullRejoin('preview_fallback');
    } finally {
      reconnecting = false;
    }
    return;
  }

  if (state === 'disconnected' || state === 'signin' || state === 'unknown') {
    unknownStreak = 0;
    console.log('[bridge] detected', state, '— full rejoin');
    emit({ type: 'disconnected', reason: state, meeting_state: state });
    reconnecting = true;
    emit({ type: 'reconnecting', strategy: 'full_rejoin' });
    try {
      await fullRejoin(state);
    } finally {
      reconnecting = false;
    }
  }
}

async function fullRejoin(reason) {
  if (!lastJoin) {
    console.log('[bridge] no lastJoin params — cannot rejoin');
    return;
  }
  console.log('[bridge] full rejoin after', reason);
  stopWatchdog();
  try {
    if (page && !page.isClosed()) {
      try {
        await page.close();
      } catch {}
    }
    page = null;
    if (browser) {
      try {
        await browser.close();
      } catch {}
      browser = null;
    }
  } catch {}
  await doJoin(lastJoin, { force: true });
}

async function doJoin(params, { force = false } = {}) {
  if (pastBridgeEnd()) {
    meetingState = 'disconnected';
    throw new Error('webinar end time reached; join disabled');
  }
  const {
    meeting_id,
    password = '',
    display_name = 'Heimdall AI',
    webinar_token = '',
    join_url = '',
  } = params;

  if (!meeting_id && !join_url) {
    throw new Error('meeting_id or join_url required');
  }

  lastJoin = {
    meeting_id,
    password,
    display_name,
    webinar_token,
    join_url,
  };

  // Soft reconnect: page alive but on preview — just click Join
  if (!force && page && !page.isClosed()) {
    const st = await detectMeetingState(page);
    meetingState = st;
    if (st === 'in_meeting' || st === 'waiting') {
      console.log('[bridge] already in meeting — ignoring /join');
      return;
    }
    if (st === 'preview') {
      console.log('[bridge] page on preview — clicking Join instead of relaunch');
      joining = true;
      meetingState = 'joining';
      try {
        await clickPreviewJoin(page);
        await page.waitForTimeout(6000);
        meetingState = await detectMeetingState(page);
        if (meetingState === 'in_meeting' || meetingState === 'waiting') {
          await postJoinSetup(page);
          emit({ type: 'reconnected', meeting_state: meetingState });
          return;
        }
      } finally {
        joining = false;
      }
      // else continue into full launch below
    }
  }

  if (joining) {
    console.log('[bridge] join already in progress, ignoring');
    return;
  }

  console.log('[bridge] starting background join for meeting', meeting_id || join_url);
  joining = true;
  meetingState = 'joining';
  joinStartedAt = Date.now();
  lastJoinFinishedAt = null;
  lastJoinError = null;

  try {
    console.log('[bridge] launching Chrome...');
    const portNum = parseInt(process.env.BRIDGE_PORT || '8765', 10);
    // Stagger windows so 5 parallel bots are visible (2-column grid)
    const slotIndex = Math.max(0, portNum - 8765);
    const winX = 40 + (slotIndex % 3) * 80;
    const winY = 40 + Math.floor(slotIndex / 3) * 60;
    const launchArgs = [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--use-fake-ui-for-media-stream',
      '--use-fake-device-for-media-stream',
      // Keep the window findable when running many parallel bots
      `--window-position=${winX},${winY}`,
      '--window-size=1200,780',
    ];
    // Prefer system Chrome (stable on Apple Silicon). Fall back to Playwright Chromium.
    try {
      browser = await chromium.launch({
        channel: 'chrome',
        headless: HEADLESS,
        args: launchArgs,
      });
    } catch (e) {
      console.log('[bridge] system Chrome unavailable, using Playwright Chromium:', e.message);
      browser = await chromium.launch({
        headless: HEADLESS,
        args: launchArgs,
      });
    }

    const context = await browser.newContext({
      permissions: ['microphone', 'camera'],
      userAgent:
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    });

    context.on('page', (p) => {
      console.log('[bridge] context opened new page');
      page = p;
    });

    page = await context.newPage();

    // Prefer the web client URL so we skip the "Open Zoom Workplace app?" interstitial.
    // Panelist /w/ links always show that page; /wc/ goes straight into browser join.
    let url = (join_url || '').trim();
    const tkFromUrl = (() => {
      try {
        return url ? new URL(url).searchParams.get('tk') || '' : '';
      } catch {
        return '';
      }
    })();
    const token = webinar_token || tkFromUrl;
    const idMatch = url.match(/\/(?:w|j|s|wc\/join|wc)\/(\d+)/);
    const mid = meeting_id || (idMatch ? idMatch[1] : '');

    if (mid && token) {
      url = `https://app.zoom.us/wc/${mid}/join?tk=${encodeURIComponent(token)}`;
      if (password) url += `&pwd=${encodeURIComponent(password)}`;
    } else if (!url) {
      if (token && mid) {
        url = `https://app.zoom.us/wc/${mid}/join?tk=${encodeURIComponent(token)}`;
        if (password) url += `&pwd=${encodeURIComponent(password)}`;
      } else if (mid) {
        url = `https://app.zoom.us/wc/join/${mid}`;
        if (password) url += `?pwd=${encodeURIComponent(password)}`;
      }
    } else if (/zoom\.us\/w\//i.test(url) && mid) {
      // Last resort: rewrite /w/ → /wc/ even without tk
      url = `https://app.zoom.us/wc/${mid}/join`;
      if (password) url += `?pwd=${encodeURIComponent(password)}`;
    }

    console.log('[bridge] navigating to', url);
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForTimeout(2000);

    // If Zoom still shows the app/browser chooser, always pick browser.
    try {
      const browserJoin = page
        .locator(
          [
            'a:has-text("Join from your browser")',
            'a:has-text("Join from Browser")',
            'button:has-text("Join from browser")',
            'a:has-text("Join from browser")',
            'button:has-text("Join from Browser")',
            'a#joinBtn',
            'a[href*="/wc/"]',
          ].join(', ')
        )
        .first();
      if (await browserJoin.isVisible({ timeout: 5000 }).catch(() => false)) {
        await browserJoin.click({ force: true });
        console.log('[bridge] clicked Join from browser');
        await page.waitForTimeout(2500);
      }
    } catch (e) {
      console.log('[bridge] no browser-join link:', e.message);
    }

    if (!page || page.isClosed()) {
      const pages = context.pages().filter((p) => !p.isClosed());
      if (pages.length) page = pages[pages.length - 1];
    }

    // Name field is optional — always try Join (preview page after disconnect has no name)
    try {
      await page.waitForSelector(
        'button.preview-join-button, input[placeholder*="name" i], #inputname',
        { timeout: 12000 }
      );
    } catch {}
    await fillNameAndJoin(page, display_name, password);
    console.log('[bridge] clicked join button (or preview Join)');

    console.log('[bridge] waiting for meeting to load...');
    if (!page || page.isClosed()) {
      const pages = context.pages().filter((p) => !p.isClosed());
      page = pages.length ? pages[pages.length - 1] : null;
    }
    if (!page) throw new Error('no active page after join navigation');

    // Poll until in meeting / waiting / or timeout
    for (let i = 0; i < 20; i++) {
      await page.waitForTimeout(1500);
      meetingState = await detectMeetingState(page);
      if (meetingState === 'in_meeting' || meetingState === 'waiting') break;
      if (meetingState === 'preview') {
        await clickPreviewJoin(page);
      }
    }

    await postJoinSetup(page);
    emit({ type: 'joined', meeting_state: meetingState });
  } catch (err) {
    console.error('[bridge] join error:', err.message);
    lastJoinError = err.message || String(err);
    meetingState = 'disconnected';
    emit({ type: 'disconnected', reason: 'join_error', error: err.message });
    // Keep watchdog trying if we have params
    startWatchdog();
  } finally {
    joining = false;
    lastJoinFinishedAt = Date.now();
  }
}

// ---- Join / Reconnect endpoints -----------------------------------------

app.post('/join', async (req, res) => {
  const {
    meeting_id,
    password = '',
    display_name = 'Heimdall AI',
    webinar_token = '',
    join_url = '',
  } = req.body;

  if (!meeting_id && !join_url) {
    return res.status(400).json({ error: 'meeting_id or join_url required' });
  }

  res.json({ ok: true });
  doJoin({
    meeting_id,
    password,
    display_name,
    webinar_token,
    join_url,
  }).catch((e) => console.error('[bridge] /join failed:', e.message));
});

app.post('/reconnect', async (req, res) => {
  res.json({ ok: true, meeting_state: meetingState });
  if (!lastJoin) {
    console.log('[bridge] /reconnect but no lastJoin — nothing to do');
    return;
  }
  recoverIfNeeded().catch((e) =>
    console.error('[bridge] /reconnect failed:', e.message)
  );
});

app.post('/focus', async (req, res) => {
  if (!page || page.isClosed()) {
    return res.status(400).json({ error: 'no page' });
  }
  try {
    await page.bringToFront();
    await page.evaluate(() => window.focus());
    const title = await page.title();
    console.log('[bridge] focused page:', title);
    res.json({ ok: true, title });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ---- Polls / Quizzes ----------------------------------------------------

async function openPollsPanel(pg) {
  const openSel = '#polling-window, [aria-label="Polls/Quizzes"]';
  const already = await pg.$(openSel);
  if (already) {
    const vis = await already.isVisible().catch(() => true);
    if (vis) {
      console.log('[bridge] polls panel already open');
      return true;
    }
  }

  // Footer control is often a button/label "Polls/Quizzes"
  const clicked = await pg.evaluate(() => {
    const nodes = [...document.querySelectorAll('button, [role="button"], a, div, span')];
    const el = nodes.find((e) => {
      const t = (e.innerText || '').replace(/\s+/g, ' ').trim();
      const aria = (e.getAttribute('aria-label') || '').trim();
      return (
        t === 'Polls/Quizzes' ||
        t === 'Polls' ||
        /^polls(\/quizzes)?$/i.test(aria)
      );
    });
    if (!el) return false;
    el.click();
    return true;
  });
  if (!clicked) {
    // Playwright text locator fallback
    try {
      await pg.locator('button:has-text("Polls"), button:has-text("Polls/Quizzes")').first().click({ timeout: 3000 });
    } catch {
      throw new Error('Could not find Polls/Quizzes button');
    }
  }
  await pg.waitForSelector(openSel, { timeout: 10000 });
  await pg.waitForTimeout(800);
  console.log('[bridge] opened polls panel');
  return true;
}

async function selectPollByName(pg, name) {
  const target = (name || '').trim();
  if (!target) throw new Error('poll name required');
  const targetFold = target.toLowerCase();

  const readHeader = async () => {
    try {
      return (
        (await pg.locator('.poll__header-name').first().innerText({ timeout: 2000 })) || ''
      )
        .replace(/\s+/g, ' ')
        .trim();
    } catch {
      return '';
    }
  };

  // Already selected?
  let current = await readHeader();
  if (current && current.toLowerCase() === targetFold) {
    console.log('[bridge] poll already selected:', current);
    return true;
  }

  // Open poll picker (header title is often a dropdown)
  try {
    await pg.locator('.poll__header-title, .poll__header-name').first().click({ timeout: 3000 });
    await pg.waitForTimeout(600);
  } catch {}

  const selected = await pg.evaluate((want) => {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const wantN = norm(want);
    const wantL = wantN.toLowerCase();

    const selectors = [
      '[class*="poll"] [role="option"]',
      '[class*="poll"] [role="menuitem"]',
      '[class*="dropdown"] [role="menuitem"]',
      '[class*="poll-list"] *',
      '[class*="polling-list"] *',
      '[class*="poll-item"]',
      'a.dropdown-item',
      '[class*="menu-item"]',
      'li',
    ];
    const seen = new Set();
    for (const sel of selectors) {
      for (const el of document.querySelectorAll(sel)) {
        if (seen.has(el)) continue;
        seen.add(el);
        const t = norm(el.innerText);
        if (!t || t.length > 200) continue;
        const first = norm(t.split('\n')[0]);
        const fl = first.toLowerCase();
        if (
          t.toLowerCase() === wantL ||
          fl === wantL ||
          fl.startsWith(wantL) ||
          wantL.startsWith(fl) ||
          fl.includes(wantL) ||
          wantL.includes(fl)
        ) {
          el.click();
          return { ok: true, via: sel, text: first };
        }
      }
    }

    const win = document.querySelector('#polling-window') || document.body;
    const leaves = [...win.querySelectorAll('*')].filter(
      (n) => n.children.length === 0 && norm(n.textContent).toLowerCase() === wantL
    );
    if (leaves[0]) {
      (
        leaves[0].closest('[role="option"], [role="menuitem"], li, a, button, div') ||
        leaves[0]
      ).click();
      return { ok: true, via: 'leaf', text: wantN };
    }
    return { ok: false };
  }, target);

  await pg.waitForTimeout(600);
  current = await readHeader();
  if (current && current.toLowerCase() === targetFold) {
    console.log('[bridge] poll selected (header match):', current);
    return true;
  }
  if (current && current.toLowerCase().includes(targetFold)) {
    console.log('[bridge] poll header contains name:', current);
    return true;
  }

  if (!selected.ok) {
    throw new Error(
      `Poll not found (exact name): ${target}` +
        (current ? ` (showing: ${current})` : ' (no poll header)')
    );
  }
  console.log('[bridge] selected poll:', selected.text, 'via', selected.via);
  return true;
}

async function clickLabeledAction(pg, labels) {
  // Prefer actions inside the polls window
  const hit = await pg.evaluate((labs) => {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const win = document.querySelector('#polling-window') || document.body;
    const nodes = [
      ...win.querySelectorAll('button, a, [role="button"], .zm-btn'),
      ...win.querySelectorAll('span, div'),
    ];
    for (const lab of labs) {
      for (const n of nodes) {
        const t = norm(n.innerText);
        if (!t || t.length > 40) continue;
        if (t.toLowerCase() === lab.toLowerCase()) {
          // Prefer the clickable ancestor if this is a label span
          const clickable =
            n.closest('button, a, [role="button"], .zm-btn') || n;
          clickable.click();
          return t;
        }
      }
    }
    return null;
  }, labels);

  if (hit) {
    console.log('[bridge] clicked poll action:', hit);
    await pg.waitForTimeout(800);
    return hit;
  }
  return null;
}

async function launchPoll(pg, name) {
  await openPollsPanel(pg);
  await selectPollByName(pg, name);
  const clicked = await clickLabeledAction(pg, [
    'Launch',
    'Launch Poll',
    'Launch Quiz',
    'Start',
    'Start Poll',
  ]);
  if (!clicked) {
    // Host-only control may be missing for panelists
    const status = await pg
      .locator('.poll__header-status')
      .first()
      .innerText()
      .catch(() => '');
    throw new Error(
      `Could not find Launch for "${name}" (status=${status || '?'}). ` +
        'Bot likely needs host/co-host (or poll host) permission.'
    );
  }
  return { ok: true, action: clicked, poll: name };
}

async function endPoll(pg, name) {
  await openPollsPanel(pg);
  if (name) {
    try {
      await selectPollByName(pg, name);
    } catch (e) {
      console.log('[bridge] end poll select skipped:', e.message);
    }
  }
  const clicked = await clickLabeledAction(pg, [
    'End Poll',
    'End Quiz',
    'End',
    'End Polling',
    'Stop Poll',
  ]);
  if (!clicked) {
    throw new Error(
      `Could not find End Poll${name ? ` for "${name}"` : ''}. ` +
        'Bot likely needs host/co-host (or poll host) permission.'
    );
  }
  return { ok: true, action: clicked, poll: name || null };
}

app.post('/poll/launch', async (req, res) => {
  if (!page) return res.status(400).json({ error: 'not in meeting' });
  const name = (req.body && (req.body.name || req.body.poll)) || '';
  if (!name) return res.status(400).json({ error: 'name required' });
  try {
    const result = await launchPoll(page, name);
    emit({ type: 'poll_launched', poll: name });
    res.json(result);
  } catch (err) {
    console.error('[bridge] poll launch error:', err.message);
    res.status(500).json({ error: err.message });
  }
});

app.post('/poll/end', async (req, res) => {
  if (!page) return res.status(400).json({ error: 'not in meeting' });
  const name = (req.body && (req.body.name || req.body.poll)) || '';
  try {
    const result = await endPoll(page, name);
    emit({ type: 'poll_ended', poll: name || null });
    res.json(result);
  } catch (err) {
    console.error('[bridge] poll end error:', err.message);
    res.status(500).json({ error: err.message });
  }
});

app.post('/chat/settings/panelists_everyone', async (req, res) => {
  if (!page) return res.status(400).json({ error: 'not in meeting' });
  try {
    const result = await ensurePanelistsChatWithEveryone(page);
    emit({ type: 'chat_settings', ...result });
    if (result.ok) return res.json(result);
    return res.status(403).json(result);
  } catch (err) {
    console.error('[bridge] chat settings error:', err.message);
    res.status(500).json({ ok: false, error: err.message });
  }
});

// ---- Select recipient in chat dropdown ----------------------------------

async function selectRecipient(pg, to) {
  const toggle = await pg.$(
    '.chat-receiver-list__receiver, [class*="chat-receiver-list__receiver"]'
  );
  if (!toggle) {
    console.log('[bridge] no recipient dropdown found');
    return false;
  }

  await toggle.click();
  await pg.waitForTimeout(500);

  const items = await pg.$$(
    'a.chat-receiver-list__menu-item, [class*="chat-receiver-list__menu-item"]'
  );
  const target = to.toLowerCase().trim();

  for (const item of items) {
    const txt = ((await item.innerText()) || '').toLowerCase().trim();
    const matched =
      txt === target ||
      txt.startsWith(target.slice(0, 12)) ||
      target.startsWith(txt.slice(0, 12));
    if (matched && txt !== 'hosts and panelists' && txt !== 'everyone') {
      await item.click();
      console.log('[bridge] selected recipient:', txt);
      await pg.waitForTimeout(300);
      return true;
    }
  }

  await pg.keyboard.press('Escape');
  console.log('[bridge] recipient not in dropdown:', to);
  return false;
}

// ---- Send chat ----------------------------------------------------------

// Serialize chat sends — concurrent type() calls scramble Zoom's textarea.
let sendChatChain = Promise.resolve();

app.post('/send_chat', async (req, res) => {
  if (!page) return res.status(400).json({ error: 'not in meeting' });

  const { text, to = 'everyone', submit = true } = req.body;
  if (!text) return res.status(400).json({ error: 'text required' });
  const shouldSubmit = submit !== false && submit !== 'false';

  const run = async () => {
  try {
    // If we're on the Join preview, recover before sending
    const st = await detectMeetingState(page);
    meetingState = st;
    if (st === 'preview' || st === 'disconnected') {
      console.log('[bridge] send_chat saw', st, '— recovering first');
      await recoverIfNeeded();
      await page.waitForTimeout(2000);
    }

    await openChatPanel(page);
    await page.waitForTimeout(400);

    if (to && to.toLowerCase() !== 'everyone') {
      const ok = await selectRecipient(page, to);
      if (!ok) {
        console.log('[bridge] falling back to everyone (recipient not found)');
      }
    } else {
      try {
        const toggle = await page.$(
          '.chat-receiver-list__receiver, [class*="chat-receiver-list__receiver"]'
        );
        if (toggle) {
          const cur = ((await toggle.innerText()) || '').toLowerCase();
          if (!cur.includes('everyone')) {
            await toggle.click();
            await page.waitForTimeout(300);
            const items = await page.$$(
              'a.chat-receiver-list__menu-item, [class*="chat-receiver-list__menu-item"]'
            );
            for (const item of items) {
              const txt = ((await item.innerText()) || '').toLowerCase().trim();
              if (txt === 'everyone') {
                await item.click();
                console.log('[bridge] selected Everyone');
                await page.waitForTimeout(200);
                break;
              }
            }
          }
        }
      } catch (e) {
        console.log('[bridge] everyone select skipped:', e.message);
      }
    }

    const inputSel = [
      'textarea.chat-box__chat-textarea',
      'textarea[class*="chat-box__chat-textarea"]',
      'textarea[class*="chat"]',
      'div.chat-box__chat-textarea[contenteditable="true"]',
      '[class*="chat-box__chat-textarea"][contenteditable="true"]',
      '[contenteditable="true"]',
    ].join(', ');

    const input = await page.$(inputSel);
    if (!input) throw new Error('Could not find chat input');

    await input.click({ clickCount: 3 });
    await page.keyboard.press('Backspace');
    await page.waitForTimeout(80);

    // Paste in one shot (avoids slow keystroke races / garbled chat)
    await page.evaluate((msg) => {
      const el =
        document.querySelector('textarea.chat-box__chat-textarea') ||
        document.querySelector('textarea[class*="chat"]') ||
        document.querySelector('[contenteditable="true"]');
      if (!el) return;
      el.focus();
      if (typeof el.value === 'string') {
        const proto = window.HTMLTextAreaElement.prototype;
        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
        if (desc && desc.set) desc.set.call(el, msg);
        else el.value = msg;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
      } else {
        el.textContent = msg;
        el.dispatchEvent(new InputEvent('input', { bubbles: true, data: msg }));
      }
    }, text);
    await page.waitForTimeout(120);

    if (!shouldSubmit) {
      console.log('[bridge] drafted chat to', to, '(human submit)');
      res.json({ ok: true, drafted: true });
      return;
    }

    await page.keyboard.press('Enter');
    await page.waitForTimeout(400);

    const stillThere = await page.evaluate((msg) => {
      const el =
        document.querySelector(
          'textarea.chat-box__chat-textarea, textarea[class*="chat"]'
        ) ||
        document.querySelector('[class*="chat-box__chat-textarea"]') ||
        document.querySelector('[contenteditable="true"]');
      if (!el) return false;
      const val = (el.value != null ? el.value : el.innerText || '').trim();
      return val === msg.trim() || val.includes(msg.trim());
    }, text);

    if (stillThere) {
      console.log('[bridge] Enter did not send — trying Send button');
      const sendSelectors = [
        'button[aria-label*="send" i]',
        'button.chat-box__send-btn',
        '[class*="chat-box"] button[class*="send"]',
        'button:has-text("Send")',
      ];
      let clicked = false;
      for (const sel of sendSelectors) {
        try {
          const btn = await page.$(sel);
          if (btn) {
            await btn.click();
            clicked = true;
            console.log('[bridge] clicked send via', sel);
            break;
          }
        } catch {}
      }
      if (!clicked) {
        await page.keyboard.press(
          process.platform === 'darwin' ? 'Meta+Enter' : 'Control+Enter'
        );
        console.log('[bridge] pressed modifier+Enter fallback');
      }
      await page.waitForTimeout(300);
    }

    console.log('[bridge] sent chat to', to);
    res.json({ ok: true });
  } catch (err) {
    console.error('[bridge] send_chat error:', err.message);
    res.status(500).json({ error: err.message });
  }
  };

  const waitFor = sendChatChain;
  let release;
  sendChatChain = new Promise((r) => {
    release = r;
  });
  await waitFor;
  try {
    await run();
  } finally {
    release();
  }
});

// ---- Leave --------------------------------------------------------------

app.post('/leave', async (req, res) => {
  stopWatchdog();
  lastJoin = null;
  try {
    if (page) {
      try {
        await page.click('[aria-label*="leave" i], button:has-text("Leave")', {
          timeout: 3000,
        });
        await page.waitForTimeout(1000);
      } catch {}
      await page.close();
      page = null;
    }
    if (browser) {
      await browser.close();
      browser = null;
    }
    meetingState = 'idle';
    emit({ type: 'meeting_ended' });
  } catch {}
  res.json({ ok: true });
});

// ---- Runtime eval (debug) -----------------------------------------------

app.post('/eval', async (req, res) => {
  if (!page) return res.status(400).json({ error: 'not in meeting' });
  const { code } = req.body;
  if (!code) return res.status(400).json({ error: 'code required' });
  try {
    const result = await page.evaluate(new Function(code));
    res.json({ ok: true, result });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ---- Start --------------------------------------------------------------

const PORT = parseInt(process.env.BRIDGE_PORT || '8765');
const HEADLESS = process.env.HERMES_WORKER_MODE === '1' || process.env.ZOOM_HEADLESS === '1';
app.listen(PORT, '127.0.0.1', () => {
  console.log(`[node-bridge] listening on 127.0.0.1:${PORT}`);
});

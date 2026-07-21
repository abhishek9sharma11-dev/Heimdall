/**
 * node-bridge — Playwright-based Zoom bridge for Mac.
 * Replaces the C++ Linux SDK bridge.
 * Same HTTP/SSE interface on port 8765 so Python orchestrator is unchanged.
 */

const { chromium } = require('playwright');
const express = require('express');

// Prevent crashes from unhandled promise rejections
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

// ---- SSE helpers --------------------------------------------------------

function emit(obj) {
  const line = `data: ${JSON.stringify(obj)}\n\n`;
  sseClients.forEach(res => res.write(line));
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
  res.json({ status: 'ok', in_meeting: !!page });
});

// ---- Open chat panel ----------------------------------------------------

async function openChatPanel(pg) {
  // If chat panel is already open (textarea visible), do nothing
  const alreadyOpen = await pg.$('textarea.chat-box__chat-textarea, [class*="chat-box__chat-textarea"]');
  if (alreadyOpen) {
    console.log('[bridge] chat panel already open');
    return true;
  }

  const chatSelectors = [
    'button.footer-button__button[aria-label*="chat" i]',
    'button.footer-button__button',
    '.footer-chat-button button:not([class*="toggle"]):not([class*="settings"])',
    '[class*="footer-chat-button"] button:first-child',
    'button[aria-label="open the chat pane"]',
    'button[aria-label="Chat"]',
  ];
  for (const sel of chatSelectors) {
    try {
      const el = await pg.$(sel);
      if (el) {
        await el.click();
        console.log('[bridge] opened chat panel via:', sel);
        await pg.waitForTimeout(1500);
        return true;
      }
    } catch {}
  }
  console.log('[bridge] could not find chat button — will rely on tip monitor');
  return false;
}

// ---- Inject monitors into page ------------------------------------------

async function injectMonitors(pg) {
  // Expose callback for incoming chat
  try {
    await pg.exposeFunction('__bridgeOnChat', (sender, senderId, text) => {
      if (!text || !text.trim()) return;
      const t = text.trim();
      const cleanSender = (sender || '')
        .replace(/\s+to\s+.*$/i, '') // "Alice To Everyone" / "Alice To Hosts and Panelists"
        .trim();
      // Filter garbage: skip if text is just the sender name, a timestamp, or very short
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
    // May already be exposed from a previous inject — that's fine
    console.log('[bridge] __bridgeOnChat already exposed or error:', e.message);
  }

  // Debug: dump chat-related DOM elements
  pg.on('console', msg => {
    const t = msg.text();
    if (t.startsWith('[ZOOM-DOM]') || t.startsWith('[bridge]')) console.log('[bridge-page]', t);
  });

  await pg.evaluate(() => {
    // ---- Debug dump (runs once after 3s) --------------------------------
    setTimeout(() => {
      const els = document.querySelectorAll(
        '[class*="chat"], [role="log"], [role="list"], [aria-label*="chat" i], .last-chat-message-tip__container'
      );
      els.forEach((el, i) => {
        if (i < 15) console.log(
          `[ZOOM-DOM] ${i} ${el.tagName}.${[...el.classList].join('.')} ` +
          `text="${(el.innerText || '').slice(0, 80).replace(/\n/g, ' ')}"`
        );
      });
    }, 3000);

    // ---- Strategy 1: floating "last message" tip -------------------------
    // This element appears whenever someone sends a message — no panel needed.
    // Structure: .last-chat-message-tip__container
    //              .last-chat-message-tip__from-to  → "From: Alice To: Everyone"
    //              .last-chat-message-tip__content  → message text
    {
      const seen = new Set();

      function checkTip() {
        const tip = document.querySelector(
          '.last-chat-message-tip__container, [class*="last-chat-message-tip"]'
        );
        if (!tip) return;

        // sender
        const fromEl = tip.querySelector(
          '.last-chat-message-tip__from-to, [class*="from-to"], [class*="sender"]'
        );
        // text
        const contentEl = tip.querySelector(
          '.last-chat-message-tip__content, [class*="tip__content"], [class*="message-content"]'
        );

        if (!fromEl || !contentEl) {
          // Fallback: try to parse the whole tip text
          const full = tip.innerText || '';
          const lines = full.split('\n').map(s => s.trim()).filter(Boolean);
          if (lines.length >= 2) {
            const sender = lines[0].replace(/^from:\s*/i, '').replace(/\s*to:.*/i, '').trim();
            const text = lines.slice(1).join(' ').trim();
            const key = `tip:${sender}:${text}`;
            if (!seen.has(key) && text.length >= 3 && text !== sender) {
              seen.add(key);
              setTimeout(() => seen.delete(key), 30000); // expire after 30s
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

      const tipObserver = new MutationObserver(checkTip);
      tipObserver.observe(document.body, { childList: true, subtree: true, characterData: true });
      setInterval(checkTip, 500);
      console.log('[bridge] tip monitor active');
    }

    // ---- Strategy 2: full chat panel messages (if panel opens) ----------
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
          if (parsed && parsed.sender && parsed.text && parsed.text.length >= 3 && parsed.text !== parsed.sender) {
            window.__bridgeOnChat(parsed.sender, parsed.sender, parsed.text);
          }
        });
      }

      setInterval(scanPanel, 500);
      const panelObserver = new MutationObserver(scanPanel);
      panelObserver.observe(document.body, { childList: true, subtree: true });
      console.log('[bridge] panel monitor active');
    }
  });
}

// ---- Join ---------------------------------------------------------------

app.post('/join', async (req, res) => {
  const {
    meeting_id,
    password = '',
    display_name = 'Om Asnani AI',
    webinar_token = '',
  } = req.body;

  if (!meeting_id) return res.status(400).json({ error: 'meeting_id required' });

  // Respond immediately — join happens in background
  res.json({ ok: true });
  console.log('[bridge] starting background join for meeting', meeting_id);

  try {
    if (joining) {
      console.log('[bridge] join already in progress, ignoring new /join');
      return;
    }
    joining = true;

    console.log('[bridge] launching Chrome...');
    browser = await chromium.launch({
      headless: false,
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--use-fake-ui-for-media-stream',
        '--use-fake-device-for-media-stream',
      ],
    });

    const context = await browser.newContext({
      permissions: ['microphone', 'camera'],
      userAgent:
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    });

    // Zoom sometimes opens a new tab/window and closes the initial page.
    // Keep `page` pointing at the most recent live page.
    context.on('page', (p) => {
      console.log('[bridge] context opened new page');
      page = p;
    });

    page = await context.newPage();

    // Build join URL
    let url;
    if (webinar_token) {
      url = `https://app.zoom.us/wc/${meeting_id}/join?tk=${encodeURIComponent(webinar_token)}`;
    } else {
      url = `https://app.zoom.us/wc/join/${meeting_id}`;
      if (password) url += `?pwd=${encodeURIComponent(password)}`;
    }

    console.log('[bridge] navigating to', url);
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });

    // Enter display name
    try {
      // If the page was replaced/closed by a redirect, grab the latest live page.
      if (!page || page.isClosed()) {
        const pages = context.pages().filter(p => !p.isClosed());
        if (pages.length) page = pages[pages.length - 1];
      }
      await page.waitForSelector(
        'input[placeholder*="name" i], input[placeholder*="Name" i], #inputname',
        { timeout: 15000 }
      );
      await page.fill(
        'input[placeholder*="name" i], input[placeholder*="Name" i], #inputname',
        display_name
      );
      // Click join button
      await page.click(
        'button.preview-join-button, button:has-text("Join"), #joinBtn',
        { timeout: 5000 }
      );
      console.log('[bridge] clicked join button');
    } catch (e) {
      console.log('[bridge] name/join UI not found:', e.message);
    }

    // Wait for meeting to fully load
    console.log('[bridge] waiting for meeting to load...');
    if (!page || page.isClosed()) {
      const pages = context.pages().filter(p => !p.isClosed());
      page = pages.length ? pages[pages.length - 1] : null;
    }
    if (!page) throw new Error('no active page after join navigation');
    await page.waitForTimeout(10000);

    // Open chat panel (best effort — tip monitor works without it)
    await openChatPanel(page);

    // Inject all monitors
    await injectMonitors(page);

    // Don't emit meeting_ended on incidental redirects/tab swaps. We'll emit on /leave or join errors.

    console.log('[bridge] joined and monitoring chat');
  } catch (err) {
    console.error('[bridge] join error:', err.message);
    emit({ type: 'meeting_ended' });
  } finally {
    joining = false;
  }
});

// ---- Select recipient in chat dropdown ----------------------------------

async function selectRecipient(pg, to) {
  // "to" is the sender's display name from Zoom
  // Open the Send To dropdown
  const toggle = await pg.$('.chat-receiver-list__receiver, [class*="chat-receiver-list__receiver"]');
  if (!toggle) {
    console.log('[bridge] no recipient dropdown found');
    return false;
  }

  await toggle.click();
  await pg.waitForTimeout(500);

  // All menu items
  const items = await pg.$$('a.chat-receiver-list__menu-item, [class*="chat-receiver-list__menu-item"]');
  const target = to.toLowerCase().trim();

  for (const item of items) {
    const txt = ((await item.innerText()) || '').toLowerCase().trim();
    // Match: exact, or name starts with target, or target starts with name (handles truncation)
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

  // Name not found — close dropdown, log
  await pg.keyboard.press('Escape');
  console.log('[bridge] recipient not in dropdown:', to);
  return false;
}

// ---- Send chat ----------------------------------------------------------

app.post('/send_chat', async (req, res) => {
  if (!page) return res.status(400).json({ error: 'not in meeting' });

  const { text, to = 'everyone' } = req.body;
  if (!text) return res.status(400).json({ error: 'text required' });

  try {
    // Make sure chat panel is open
    await openChatPanel(page);
    await page.waitForTimeout(800);

    // Select recipient (specific person, not "everyone")
    if (to && to.toLowerCase() !== 'everyone') {
      const ok = await selectRecipient(page, to);
      if (!ok) {
        console.log('[bridge] falling back to everyone (recipient not found)');
      }
    }

    // Type and send: use execCommand (React-friendly) then real Playwright Enter
    const focused = await page.evaluate((msg) => {
      function setWithExecCommand(el, value) {
        el.focus();
        try { document.execCommand('selectAll'); } catch {}
        try { document.execCommand('insertText', false, value); } catch {}
      }

      // Prefer a real textarea if present
      const ta = document.querySelector('textarea.chat-box__chat-textarea, textarea[class*="chat"]');
      if (ta && ta.tagName === 'TEXTAREA') {
        ta.focus();
        // Set value + emit input for React-driven UIs
        ta.value = '';
        ta.dispatchEvent(new Event('input', { bubbles: true }));
        ta.value = msg;
        ta.dispatchEvent(new Event('input', { bubbles: true }));
        return true;
      }

      // Some Zoom builds use a DIV with classes matching chat-box__chat-textarea.
      const maybeInput = document.querySelector('[class*="chat-box__chat-textarea"]');
      if (maybeInput) {
        setWithExecCommand(maybeInput, msg);
        return true;
      }

      // Fallback: any visible contenteditable
      const ce = Array.from(document.querySelectorAll('[contenteditable="true"]'))
        .find((el) => {
          const r = el.getBoundingClientRect();
          return r.width > 50 && r.height > 10;
        });
      if (ce) {
        setWithExecCommand(ce, msg);
        return true;
      }

      return false;
    }, text);

    if (focused) {
      await page.waitForTimeout(200);
      await page.keyboard.press('Enter'); // Real Playwright keypress — triggers React send
      console.log('[bridge] sent chat to', to);
    } else {
      console.log('[bridge] could not find chat input');
    }

    if (!focused) throw new Error('Could not find chat input');

    res.json({ ok: true });
  } catch (err) {
    console.error('[bridge] send_chat error:', err.message);
    res.status(500).json({ error: err.message });
  }
});

// ---- Leave --------------------------------------------------------------

app.post('/leave', async (req, res) => {
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
app.listen(PORT, '127.0.0.1', () => {
  console.log(`[node-bridge] listening on 127.0.0.1:${PORT}`);
});

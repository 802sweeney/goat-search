const API_BASE = 'http://localhost:5000';
const DEFAULT_TITLE = 'Capture this job into Goat Search';

chrome.action.onClicked.addListener(async (tab) => {
  setBadge('…', '#7a7f9a', 'Capturing…');

  if (!tab.id || !tab.url || !/^https?:\/\//.test(tab.url)) {
    setResult('err', "Can't capture this page — open a real job posting first.");
    return;
  }

  let captured;
  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      // If the user has manually highlighted text on the page (e.g. to grab just the real JD
      // out of a noisy search-results/listing page — see the Hospitable incident this was built
      // for), capture exactly that selection instead of the whole page. Falls back to the
      // existing whole-page behavior when nothing is selected, so this is purely additive.
      func: () => {
        const selected = window.getSelection().toString().trim();
        return { url: location.href, text: selected || document.body.innerText, fromSelection: !!selected };
      },
    });
    captured = result;
  } catch (e) {
    setResult('err', 'Could not read this page — try reloading it first.');
    return;
  }

  let res, body;
  try {
    res = await fetch(`${API_BASE}/api/capture`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(captured),
    });
    body = await res.json();
  } catch (e) {
    setResult('err', "Couldn't reach Goat Search — is it running? (python job_checker.py)");
    return;
  }

  if (!res.ok || body.action === 'error') {
    setResult('err', body.error || 'Something went wrong.');
    return;
  }

  const title = (body.job && body.job.title) || body.title || '';
  const company = (body.job && body.job.company_name) || body.company_name || '';
  const label = title && company ? `${title} — ${company}` : (title || 'this job');
  const selectionNote = captured.fromSelection ? ' (from your selected text)' : '';

  const reasonLabel = body.reason === 'not_remote' ? 'not remote' : 'not software-relevant';
  const messages = {
    attached: `Attached description to "${label}"${selectionNote}.`,
    matched:  `Matched by title/company — attached to "${label}"${selectionNote}.`,
    created:  `Created new tracked job: "${label}"${selectionNote}.`,
    excluded: `Not saved — ${reasonLabel}: ${body.reasoning || ''}`,
  };

  if (body.action === 'excluded') {
    setResult('warn', messages.excluded);
  } else {
    setResult('ok', messages[body.action] || 'Captured.');
  }
});

// Badge rendered directly by Chrome on the toolbar icon — not routed through the OS
// notification system at all, so it can't be silenced by macOS notification permissions
// the way chrome.notifications can.
function setBadge(text, color, title) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setTitle({ title });
}

function setResult(kind, message) {
  const colors = { ok: '#3ecf8e', warn: '#f4c54a', err: '#f86a6a' };
  const badgeText = { ok: '✓', warn: '–', err: '✕' };
  setBadge(badgeText[kind], colors[kind], message);
  // Clear the badge after a while so it doesn't stay stuck forever; the last message stays
  // as the tooltip a bit longer in case the user checks back before hovering.
  setTimeout(() => chrome.action.setBadgeText({ text: '' }), 8000);
  setTimeout(() => chrome.action.setTitle({ title: DEFAULT_TITLE }), 30000);
}

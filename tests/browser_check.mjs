// Drives headless Chrome over the DevTools protocol (Node 22+, no packages)
// to check the game's video player against a running Moviedle server.
//
// Usage: node tests/browser_check.mjs <base-url> <chrome-binary> <screenshot.png>
// Prints one JSON object with layout and playback measurements.
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [baseUrl, chromePath, screenshotPath] = process.argv.slice(2);
const profile = mkdtempSync(join(tmpdir(), 'moviedle-chrome-'));
const chrome = spawn(chromePath, [
    '--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--mute-audio',
    '--window-size=1400,1000', 'about:blank',
], { stdio: 'ignore' });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
    const portFile = join(profile, 'DevToolsActivePort');
    for (let i = 0; i < 200 && !existsSync(portFile); i++) await sleep(50);
    await sleep(100);
    const port = readFileSync(portFile, 'utf8').split('\n')[0];
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const ws = new WebSocket(targets.find((target) => target.type === 'page').webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });

    let nextId = 0;
    const pending = new Map();
    ws.onmessage = (event) => {
        const message = JSON.parse(event.data);
        if (pending.has(message.id)) {
            pending.get(message.id)(message);
            pending.delete(message.id);
        }
    };
    const send = (method, params = {}) => new Promise((resolve) => {
        const id = ++nextId;
        pending.set(id, resolve);
        ws.send(JSON.stringify({ id, method, params }));
    });
    const evaluate = async (expression) => {
        const { result } = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
        if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
        return result.result.value;
    };

    await send('Page.navigate', { url: baseUrl });
    for (let i = 0; i < 100 && await evaluate('document.readyState') !== 'complete'; i++) await sleep(50);

    const helpers = `
        const waitFor = (check, ms = 15000) => new Promise((resolve) => {
            const start = performance.now();
            (function poll() {
                if (check()) return resolve(true);
                if (performance.now() - start > ms) return resolve(false);
                setTimeout(poll, 50);
            })();
        });
        const box = (el) => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height}; };
        const video = document.getElementById('game-video');
        const message = document.getElementById('clip-message');
    `;

    const good = await evaluate(`(async () => {
        ${helpers}
        const button = document.querySelector('.btn.play.easy');
        await waitFor(() => !button.disabled);
        button.click();
        const played = await waitFor(() => video.readyState >= 2 && video.currentTime > 0.3);
        const screen = box(document.querySelector('.screen'));
        const element = box(video);
        const style = getComputedStyle(video);
        // Where the picture is drawn: object-fit:contain letterboxes it inside
        // the element; without it (the old bug) it fills the element box.
        let picture = element;
        if (style.objectFit === 'contain' && video.videoWidth) {
            const scale = Math.min(element.w / video.videoWidth, element.h / video.videoHeight);
            const w = video.videoWidth * scale, h = video.videoHeight * scale;
            picture = {x: element.x + (element.w - w) / 2, y: element.y + (element.h - h) / 2, w, h};
        }
        const overlapW = Math.max(0, Math.min(picture.x + picture.w, screen.x + screen.w) - Math.max(picture.x, screen.x));
        const overlapH = Math.max(0, Math.min(picture.y + picture.h, screen.y + screen.h) - Math.max(picture.y, screen.y));
        return {
            played,
            src: video.currentSrc,
            error: video.error && video.error.code,
            videoWidth: video.videoWidth,
            videoHeight: video.videoHeight,
            screen, element, picture,
            visibleFraction: (overlapW * overlapH) / (picture.w * picture.h),
            objectFit: style.objectFit,
            transform: style.transform,
            animationName: style.animationName,
            filter: style.filter,
            messageHidden: message.hidden,
            startResponseKeys: null,
        };
    })()`);

    const clip = await send('Page.captureScreenshot', {
        format: 'png',
        clip: { x: good.screen.x, y: good.screen.y, width: good.screen.w, height: good.screen.h, scale: 1 },
    });
    writeFileSync(screenshotPath, Buffer.from(clip.result.data, 'base64'));

    const broken = await evaluate(`(async () => {
        ${helpers}
        await playDifficulty('medium');
        const shown = await waitFor(() => !message.hidden);
        return {
            shown,
            text: document.getElementById('clip-message-text').textContent,
            button: document.getElementById('clip-retry').textContent,
            error: video.error && video.error.code,
        };
    })()`);

    ws.close();
    return { good, broken };
}

try {
    console.log(JSON.stringify(await main()));
} finally {
    chrome.kill();
    await sleep(200);
    rmSync(profile, { recursive: true, force: true });
}

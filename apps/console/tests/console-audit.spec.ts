import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

/**
 * Independent Phase 4 audit specs against the BUILT preview (:4173).
 * Kept deliberately small: the exhaustive keyboard/axe/synthetic sweep lives in
 * tests/e2e/test_console_audit.py (same repo, pytest + Playwright). This file pins the
 * claims that must hold for the shipped artifact itself.
 */

const BANNER = "Simulation only. Not authorised for flight.";
const ARTIFACTS = path.join(path.dirname(fileURLToPath(import.meta.url)), "artifacts");
const ROUTES: Array<[string, string[]]> = [
  ["map", ["exceptions"]],
  ["missions", ["mission-queue", "mission-board"]],
  ["fleet", ["fleet"]],
  ["economics", ["economics"]],
  ["decision", ["decision-trace"]],
  ["replay", ["replay"]],
  ["comparison", ["comparison"]],
];

test.beforeAll(() => fs.mkdirSync(ARTIFACTS, { recursive: true }));

for (const [route, roots] of ROUTES) {
  test(`route ${route}: banner, panel roots, no placeholder, zero JS errors`, async ({ page }) => {
    const jsErrors: string[] = [];
    page.on("pageerror", (e) => jsErrors.push(String(e)));
    await page.goto(`/#/${route}`, { waitUntil: "load" });
    const banner = page.locator(".safety-banner").first();
    await expect(banner).toBeVisible();
    expect(await page.locator(".safety-banner").count()).toBeGreaterThanOrEqual(2);
    expect(await banner.innerText()).toContain(BANNER);
    for (const root of roots) {
      await expect(page.locator(`[data-panel-root='${root}']`)).toHaveCount(1, { timeout: 20_000 });
    }
    await page.waitForTimeout(1500);
    await expect(page.locator("body")).not.toContainText("No panel is registered");
    expect(jsErrors, `uncaught JS on ${route}: ${jsErrors.join("; ")}`).toEqual([]);
    await page.screenshot({ path: path.join(ARTIFACTS, `pw-${route}.png`) });
  });
}

test("built bundle has no fixture transport: ?transport=fixtures is inert", async ({ page }) => {
  await page.goto("/?transport=fixtures#/economics", { waitUntil: "load" });
  await expect(page.locator("[data-panel-root='economics']")).toBeVisible({ timeout: 20_000 });
  const body = (await page.innerText("body")).toLowerCase();
  expect(body).not.toContain("synthetic dev transport");
  // Inverted with the audit's F-7 correction: cost figures are REQUIRED to be labelled
  // illustrative (docs/05 5.9, docs/07 7.2), so the shipped artifact must show the label.
  expect(body).toContain("illustrative");
});

test("prefers-reduced-motion: nothing animates on the built shell", async ({ browser }) => {
  const ctx = await browser.newContext({ reducedMotion: "reduce" });
  const page = await ctx.newPage();
  await page.goto("/#/replay", { waitUntil: "load" });
  await page.waitForTimeout(2500);
  const nonzero = await page.evaluate(() =>
    [...document.querySelectorAll("body *")]
      .map((el) => {
        const cs = getComputedStyle(el);
        return [cs.transitionDuration, cs.animationDuration];
      })
      // The console zeroes motion by setting 0.000001s, not literal 0s (transitionend
      // still fires). Treat anything under 2 ms as no motion; anything above is a leak.
      .filter(([t, a]) => (t !== "0s" && parseFloat(t) > 0.002) || (a !== "0s" && parseFloat(a) > 0.002)).length,
  );
  expect(nonzero).toBe(0);
  await ctx.close();
});

test("keyboard: primary nav is reachable and shows a focus indicator", async ({ page }) => {
  await page.goto("/#/map", { waitUntil: "load" });
  await page.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur());
  let reached = false;
  let ring = false;
  for (let i = 0; i < 12 && !reached; i += 1) {
    await page.keyboard.press("Tab");
    const info = await page.evaluate(() => {
      const a = document.activeElement as HTMLElement | null;
      return {
        inNav: !!a?.closest("nav"),
        focusVisible: !!a?.matches(":focus-visible"),
        outline: (() => {
          if (!a) return "";
          const cs = getComputedStyle(a);
          return cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) > 0 ? "yes" : "no";
        })(),
      };
    });
    if (info.inNav) {
      reached = true;
      ring = info.focusVisible && info.outline === "yes";
    }
  }
  expect(reached, "no nav link within 12 tab stops").toBeTruthy();
  expect(ring, "focus ring not visible on nav link").toBeTruthy();
});

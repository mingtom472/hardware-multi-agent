import { expect, test } from "@playwright/test";
import { createHash } from "node:crypto";

test("uploads engineering files over the streaming proxy and completes the image workflow", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  const run = page.getByRole("button", { name: "开始协作分析" });
  await expect(run).toBeEnabled();
  await page.getByLabel("设计背景与分析目标").fill("评审24V转5V Buck电源的器件裕量与验证计划");
  await page.locator("#files").setInputFiles([
    // Contents are intentionally inert: the MVP vision stage consumes metadata only.
    { name: "schematic.png", mimeType: "image/png", buffer: Buffer.from("mock image fixture") },
    { name: "datasheet.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.7 mock fixture") },
    { name: "controller.c", mimeType: "text/plain", buffer: Buffer.from("int main(void) { return 0; }") },
    // Greater than Next's historical 10 MiB proxy clone limit. Administrator
    // resource limits are configured separately; uploads are never unlimited.
    { name: "capture.bin", mimeType: "application/octet-stream", buffer: Buffer.alloc(11 * 1024 * 1024, 0x42) },
  ]);
  const accepted = page.waitForResponse(response => new URL(response.url()).pathname === "/api/tasks" && response.request().method() === "POST");
  await run.click();
  const task = await (await accepted).json();
  await expect(page.locator(".status-tag")).toHaveText("分析完成");
  await expect(page.getByRole("heading", { name: "协作汇总" })).toBeVisible();
  await expect(page.locator(".summary")).toContainText("模拟汇总");
  await expect(page.locator(".call-list > details")).toHaveCount(7);
  await expect(page.locator(".event-list")).toContainText("图像识别");
  await expect(page.locator(".metrics")).toContainText("模拟费用");
  await expect(page.locator(".file-list")).toContainText("capture.bin");
  const uploaded = await (await page.request.get(`/api/uploads/${task.file_ids[3]}`)).json();
  expect(uploaded.size).toBe(11 * 1024 * 1024);
  expect(uploaded.sha256).toBe(createHash("sha256").update(Buffer.alloc(11 * 1024 * 1024, 0x42)).digest("hex"));
  expect(errors).toEqual([]);
});

test("honors the user-selected reviewers and validation model", async ({ page }) => {
  await page.goto("/");
  const run = page.getByRole("button", { name: "开始协作分析" });
  await expect(run).toBeEnabled();
  await page.locator(".model-option").filter({ hasText: "豆包" }).getByRole("checkbox").uncheck();
  await page.locator(".advanced > summary").click();
  await page.getByLabel("验证模型", { exact: true }).selectOption("deepseek");
  const accepted = page.waitForResponse(response => new URL(response.url()).pathname === "/api/tasks" && response.request().method() === "POST");
  await run.click();
  const created = await (await accepted).json();
  await expect(page.locator(".status-tag")).toHaveText("分析完成");
  const finished = await (await page.request.get(`/api/tasks/${created.id}`)).json();
  expect(finished.workflow.strategy).toBe("manual");
  expect(finished.calls.filter((call: { stage: string }) => call.stage === "hardware_analysis").map((call: { provider: string }) => call.provider).sort()).toEqual(["deepseek", "openai"]);
  expect(finished.calls.find((call: { stage: string }) => call.stage === "validation").provider).toBe("deepseek");
});

test("shows task creation errors and supports a successful retry", async ({ page }) => {
  let uploadRequests = 0;
  page.on("request", request => {
    if (new URL(request.url()).pathname === "/api/uploads" && request.method() === "POST") uploadRequests++;
  });
  await page.goto("/");
  const run = page.getByRole("button", { name: "开始协作分析" });
  await expect(run).toBeEnabled();
  await page.locator("#files").setInputFiles({
    name: "retry.c", mimeType: "text/plain", buffer: Buffer.from("// retry upload reuse fixture"),
  });
  await page.route("**/api/tasks", route => route.fulfill({
    status: 503, contentType: "application/json", body: JSON.stringify({ detail: "temporary test failure" }),
  }), { times: 1 });
  await run.click();
  await expect(page.getByRole("alert").filter({ hasText: "temporary test failure" })).toBeVisible();
  await expect(run).toBeEnabled();
  await run.click();
  await expect(page.locator(".status-tag")).toHaveText("分析完成");
  // No image attachments: classification + three reviews + validation + summary.
  await expect(page.locator(".call-list > details")).toHaveCount(6);
  expect(uploadRequests).toBe(1);
});

test("uses the configured file limit and prevents blank prompts", async ({ page }) => {
  await page.route("**/api/capabilities", async route => {
    const response = await route.fetch();
    const data = await response.json();
    data.limits.max_upload_bytes = 1024;
    await route.fulfill({ response, json: data });
  });
  await page.goto("/");
  const run = page.getByRole("button", { name: "开始协作分析" });
  await expect(run).toBeEnabled();
  await page.locator("#files").setInputFiles({
    name: "too-large.bin", mimeType: "application/octet-stream", buffer: Buffer.alloc(2048),
  });
  await expect(page.getByRole("alert").filter({ hasText: "超过单文件" })).toBeVisible();
  await expect(page.locator(".file-list")).toHaveCount(0);
  await page.getByLabel("设计背景与分析目标").fill("   ");
  await expect(run).toBeDisabled();
});

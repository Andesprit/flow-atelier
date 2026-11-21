import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import Launch from "@/pages/Launch";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("confirms an unlisted folder and retries a double-tapped launch with the same ID", async () => {
  const launches: Record<string, unknown>[] = [];
  let fail!: () => void;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
    if (url.endsWith("/mobile/projects")) return Response.json([]);
    if (url.includes("/mobile/conduits?")) return Response.json([{ name: "probe", inputs: {} }]);
    launches.push(JSON.parse(String(init.body)));
    return new Promise<Response>((_, reject) => { fail = () => reject(Error("lost response")); });
  }));
  const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValue(true);
  vi.stubGlobal("confirm", confirm);
  render(<MemoryRouter><Launch /></MemoryRouter>);
  fireEvent.change(await screen.findByLabelText("Folder on this server"), { target: { value: "/new" } });
  fireEvent.click(screen.getByRole("button", { name: "Use folder" }));
  expect(confirm.mock.calls[0][0]).toMatch(/\/new.*own recipes/i);
  expect(screen.queryByRole("button", { name: "Launch" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Use folder" }));
  const launch = await screen.findByRole("button", { name: "Launch" });
  fireEvent.click(launch);
  fireEvent.click(launch);
  expect(launches).toHaveLength(1);
  fail();
  await screen.findByRole("alert");
  fireEvent.click(launch);
  expect(launches).toHaveLength(2);
  expect(launches[0].launch_id).toMatch(/^[a-f0-9]{32}$/);
  expect(launches[1]).toEqual(launches[0]);
  expect(launches[1].confirmed_folder).toBe("/new");
});

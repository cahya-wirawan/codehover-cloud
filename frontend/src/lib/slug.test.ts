import { expect, test } from "vitest";

import { projectSlugFrom } from "./slug";

test("a project's address is made from its name like the server makes it", () => {
  expect(projectSlugFrom("My Demo App!")).toBe("my-demo-app");
  expect(projectSlugFrom("  --Micrograd--  ")).toBe("micrograd");
  expect(projectSlugFrom("日本")).toBe("");
  expect(projectSlugFrom("a".repeat(47) + " b")).toBe("a".repeat(47));
});

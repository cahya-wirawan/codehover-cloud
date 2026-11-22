import { safeReturnPath } from "./returnPath";

test("only paths on this origin are returned to", () => {
  expect(safeReturnPath("/w/abc?x=1#y")).toBe("/w/abc?x=1#y");
  for (const bad of [null, "", "https://evil.test/", "//evil.test", "/\\evil.test", "/\t/evil.test", "javascript:alert(1)", "/a\nb"]) {
    expect(safeReturnPath(bad)).toBe("/");
  }
});

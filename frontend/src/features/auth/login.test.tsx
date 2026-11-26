import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { LoginPage } from "./LoginPage";

test("a suspended account is told why it cannot sign in", () => {
  render(
    <MemoryRouter initialEntries={["/login?error=suspended"]}>
      <LoginPage />
    </MemoryRouter>,
  );
  expect(screen.getByText(/This account is suspended/)).toBeInTheDocument();
});

test("an unknown error code falls back to the provider message", () => {
  render(
    <MemoryRouter initialEntries={["/login?error=whatever"]}>
      <LoginPage />
    </MemoryRouter>,
  );
  expect(screen.getByText(/could not confirm your identity/)).toBeInTheDocument();
});

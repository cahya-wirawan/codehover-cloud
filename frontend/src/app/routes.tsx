import type { RouteObject } from "react-router";

import { AppShell } from "./AppShell";
import { HomePage, NotFoundPage, RouteError } from "./pages";

export const routes: RouteObject[] = [
  {
    path: "/",
    element: <AppShell />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <HomePage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];

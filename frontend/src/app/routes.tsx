import type { RouteObject } from "react-router";

import { LoginPage } from "../features/auth/LoginPage";
import { ProjectPage } from "../features/projects/ProjectPage";
import { BuildPage } from "../features/viewer/BuildPage";
import { AcceptInvitationPage } from "../features/workspaces/AcceptInvitationPage";
import { ConnectionsPage } from "../features/workspaces/ConnectionsPage";
import { WorkspacePage } from "../features/workspaces/WorkspacePage";
import { WorkspacesPage } from "../features/workspaces/WorkspacesPage";
import { AppShell } from "./AppShell";
import { HomePage, NotFoundPage, RouteError } from "./pages";

export const routes: RouteObject[] = [
  {
    path: "/",
    element: <AppShell />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <HomePage /> },
      { path: "login", element: <LoginPage /> },
      { path: "w", element: <WorkspacesPage /> },
      { path: "w/:wid", element: <WorkspacePage /> },
      { path: "w/:wid/connections", element: <ConnectionsPage /> },
      { path: "p/:pid", element: <ProjectPage /> },
      { path: "p/:pid/builds/:bid", element: <BuildPage /> },
      { path: "invitations/accept", element: <AcceptInvitationPage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];

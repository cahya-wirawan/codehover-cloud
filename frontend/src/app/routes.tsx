import type { RouteObject } from "react-router";

import { LoginPage } from "../features/auth/LoginPage";
import { SiteCode, SiteGuide, SitePage, SiteSearch } from "../features/sites/SitePage";
import { ProjectPage } from "../features/projects/ProjectPage";
import {
  BuildCode,
  BuildGuide,
  BuildOverview,
  BuildPage,
  BuildReview,
  BuildSearch,
} from "../features/viewer/BuildPage";
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
      {
        path: "p/:pid/builds/:bid",
        element: <BuildPage />,
        children: [
          { index: true, element: <BuildOverview /> },
          { path: "code", element: <BuildCode /> },
          { path: "guide", element: <BuildGuide /> },
          { path: "guide/:slug", element: <BuildGuide /> },
          { path: "search", element: <BuildSearch /> },
          { path: "review", element: <BuildReview /> },
        ],
      },
      { path: "invitations/accept", element: <AcceptInvitationPage /> },
      {
        path: "sites/:site",
        element: <SitePage />,
        children: [
          { index: true, element: <SiteGuide /> },
          { path: "guide", element: <SiteGuide /> },
          { path: "guide/:slug", element: <SiteGuide /> },
          { path: "code", element: <SiteCode /> },
          { path: "search", element: <SiteSearch /> },
        ],
      },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];

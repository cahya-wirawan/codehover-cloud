import type { RouteObject } from "react-router";

import { AccountPage } from "../features/account/AccountPage";
import { LoginPage } from "../features/auth/LoginPage";
import { PrivacyPage, SupportPage, TermsPage } from "../features/legal/LegalPages";
import { OpsPage } from "../features/ops/OpsPage";
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
import { AuditPage } from "../features/workspaces/AuditPage";
import { ConnectionsPage } from "../features/workspaces/ConnectionsPage";
import { MembersPage } from "../features/workspaces/MembersPage";
import { UsagePage } from "../features/workspaces/UsagePage";
import { WorkspacePage } from "../features/workspaces/WorkspacePage";
import { WorkspacesPage } from "../features/workspaces/WorkspacesPage";
import { AppShell } from "./AppShell";
import { HomePage, NotFoundPage, RouteError } from "./pages";
import { RequireSignIn } from "./RequireSignIn";

export const routes: RouteObject[] = [
  {
    path: "/",
    element: <AppShell />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <HomePage /> },
      { path: "login", element: <LoginPage /> },
      { path: "privacy", element: <PrivacyPage /> },
      { path: "terms", element: <TermsPage /> },
      { path: "support", element: <SupportPage /> },
      {
        element: <RequireSignIn />,
        children: [
          { path: "account", element: <AccountPage /> },
          { path: "ops", element: <OpsPage /> },
          { path: "w", element: <WorkspacesPage /> },
          { path: "w/:wid", element: <WorkspacePage /> },
          { path: "w/:wid/members", element: <MembersPage /> },
          { path: "w/:wid/connections", element: <ConnectionsPage /> },
          { path: "w/:wid/audit", element: <AuditPage /> },
          { path: "w/:wid/usage", element: <UsagePage /> },
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

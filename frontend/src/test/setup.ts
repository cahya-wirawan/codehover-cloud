import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";

// findBy*/waitFor wait up to 5 s (default 1 s): router-driven viewer tests render slowly when every test
// file runs at once, and a short wait made them flaky.
configure({ asyncUtilTimeout: 5000 });

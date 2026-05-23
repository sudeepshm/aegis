/**
 * middleware.ts
 * =============
 * Edge Runtime route guard for Project Aegis — Phase 5.
 *
 * Protects all protected app routes. Unauthenticated requests
 * are redirected to /login via NextAuth's authorized callback.
 */

export { auth as middleware } from "@/auth";

export const config = {
  matcher: [
    "/dashboard/:path*",
    "/dag/:path*",
    "/hitl/:path*",
    "/review/:path*",
    "/swarm/:path*",
    "/tensions/:path*",
    "/upload/:path*",
  ],
};

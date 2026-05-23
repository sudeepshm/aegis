/**
 * NextAuth API route handler — Project Aegis Phase 5.
 *
 * Exports GET and POST handlers that NextAuth uses for
 * /api/auth/signin, /api/auth/signout, /api/auth/callback, etc.
 */

import { handlers } from "@/auth";

export const { GET, POST } = handlers;

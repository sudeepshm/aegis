/**
 * auth.ts
 * =======
 * NextAuth v5 configuration for Project Aegis — Phase 5.
 *
 * Provider  : CredentialsProvider (mock users for development)
 * Strategy  : JWT (stateless — no database sessions)
 * Roles     : "admin" (full access) | "cco" (read-only)
 *
 * Usage:
 *   import { auth, signIn, signOut } from "@/auth"
 */

import NextAuth from "next-auth";
import Credentials from "next-auth/providers/credentials";

// ---------------------------------------------------------------------------
// Type augmentation for role-aware sessions
// ---------------------------------------------------------------------------

declare module "next-auth" {
  interface User {
    role: "admin" | "cco";
  }
  interface Session {
    user: {
      role: "admin" | "cco";
      id?: string;
      name?: string | null;
      email?: string | null;
      image?: string | null;
    };
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    role: "admin" | "cco";
  }
}

// ---------------------------------------------------------------------------
// Mock user database
// ---------------------------------------------------------------------------

interface MockUser {
  id: string;
  name: string;
  email: string;
  password: string;
  role: "admin" | "cco";
}

const MOCK_USERS: MockUser[] = [
  {
    id: "1",
    name: "Admin User",
    email: "admin@aegis.bank",
    password: "admin123",
    role: "admin",
  },
  {
    id: "2",
    name: "CCO Officer",
    email: "cco@aegis.bank",
    password: "cco123",
    role: "cco",
  },
];

// ---------------------------------------------------------------------------
// NextAuth configuration
// ---------------------------------------------------------------------------

export const { handlers, signIn, signOut, auth } = NextAuth({
  providers: [
    Credentials({
      name: "Aegis Credentials",
      credentials: {
        email: { label: "Email", type: "email" },
        password: { label: "Password", type: "password" },
      },
      async authorize(credentials) {
        if (!credentials?.email || !credentials?.password) {
          return null;
        }

        const user = MOCK_USERS.find(
          (u) =>
            u.email === credentials.email &&
            u.password === credentials.password
        );

        if (!user) {
          return null;
        }

        return {
          id: user.id,
          name: user.name,
          email: user.email,
          role: user.role,
        };
      },
    }),
  ],
  pages: {
    signIn: "/login",
  },
  callbacks: {
    async jwt({ token, user }) {
      // On initial sign-in, persist the role into the JWT
      if (user) {
        token.role = (user as MockUser).role;
      }
      return token;
    },
    async session({ session, token }) {
      // Expose the role in the session object
      if (session.user) {
        session.user.role = token.role;
      }
      return session;
    },
    async authorized({ auth: session, request }) {
      // Protect /dashboard/* routes at the middleware level
      const isProtected = request.nextUrl.pathname.startsWith("/dashboard") ||
                          request.nextUrl.pathname.startsWith("/dag") ||
                          request.nextUrl.pathname.startsWith("/hitl") ||
                          request.nextUrl.pathname.startsWith("/review") ||
                          request.nextUrl.pathname.startsWith("/swarm") ||
                          request.nextUrl.pathname.startsWith("/tensions") ||
                          request.nextUrl.pathname.startsWith("/upload");
      if (isProtected && !session) {
        return false; // Triggers redirect to /login
      }
      return true;
    },
  },
  session: {
    strategy: "jwt",
  },
});

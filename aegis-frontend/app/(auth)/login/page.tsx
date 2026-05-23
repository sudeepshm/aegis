/**
 * (auth)/login/page.tsx
 * =====================
 * Login page for Project Aegis — Phase 5.
 *
 * Server Component — renders a dark, premium login card.
 * Uses NextAuth's signIn server action for credential submission.
 *
 * Mock credentials:
 *   admin@aegis.bank / admin123  → Full admin access
 *   cco@aegis.bank / cco123     → Read-only CCO access
 */

import { signIn } from "@/auth";
import { redirect } from "next/navigation";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; callbackUrl?: string }>;
}) {
  const params = await searchParams;
  const errorMsg = params.error;
  const callbackUrl = params.callbackUrl || "/";

  return (
    <div className="min-h-screen flex items-center justify-center bg-[#0a0a0f] relative overflow-hidden">
      {/* Gradient orbs background */}
      <div
        className="absolute inset-0 pointer-events-none"
        aria-hidden="true"
      >
        <div className="absolute top-1/4 left-1/4 w-96 h-96 bg-cyan-500/5 rounded-full blur-3xl" />
        <div className="absolute bottom-1/4 right-1/4 w-96 h-96 bg-violet-500/5 rounded-full blur-3xl" />
      </div>

      {/* Login card */}
      <div className="relative z-10 w-full max-w-md mx-4">
        <div
          className="rounded-2xl border border-white/[0.06] p-8"
          style={{
            background:
              "linear-gradient(145deg, rgba(15,15,25,0.95) 0%, rgba(10,10,20,0.98) 100%)",
            backdropFilter: "blur(24px)",
            boxShadow:
              "0 0 0 1px rgba(255,255,255,0.03), " +
              "0 25px 50px -12px rgba(0,0,0,0.5), " +
              "inset 0 1px 0 rgba(255,255,255,0.03)",
          }}
        >
          {/* Logo / Title */}
          <div className="text-center mb-8">
            <div
              className="inline-flex items-center justify-center w-14 h-14 rounded-xl mb-4"
              style={{
                background:
                  "linear-gradient(135deg, rgba(6,182,212,0.15) 0%, rgba(139,92,246,0.15) 100%)",
                border: "1px solid rgba(6,182,212,0.2)",
              }}
            >
              <svg
                width="28"
                height="28"
                viewBox="0 0 24 24"
                fill="none"
                stroke="url(#aegis-gradient)"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <defs>
                  <linearGradient id="aegis-gradient" x1="0" y1="0" x2="24" y2="24">
                    <stop offset="0%" stopColor="#06b6d4" />
                    <stop offset="100%" stopColor="#8b5cf6" />
                  </linearGradient>
                </defs>
                <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
              </svg>
            </div>
            <h1
              className="text-2xl font-bold tracking-tight mb-1"
              style={{
                background: "linear-gradient(135deg, #e2e8f0 0%, #94a3b8 100%)",
                WebkitBackgroundClip: "text",
                WebkitTextFillColor: "transparent",
              }}
            >
              Project Aegis
            </h1>
            <p className="text-sm text-gray-500">
              Enterprise Compliance Intelligence
            </p>
          </div>

          {/* Error banner */}
          {errorMsg && (
            <div className="mb-6 px-4 py-3 rounded-lg bg-red-500/10 border border-red-500/20">
              <p className="text-sm text-red-400">
                Authentication failed. Please check your credentials.
              </p>
            </div>
          )}

          {/* Sign-in form */}
          <form
            action={async (formData: FormData) => {
              "use server";
              try {
                await signIn("credentials", {
                  email: formData.get("email") as string,
                  password: formData.get("password") as string,
                  redirectTo: callbackUrl,
                });
              } catch (error) {
                // NextAuth throws a redirect on success — only real errors reach here
                throw error;
              }
            }}
            className="space-y-5"
          >
            {/* Email */}
            <div>
              <label
                htmlFor="email"
                className="block text-xs font-medium text-gray-400 mb-2 uppercase tracking-wider"
              >
                Email
              </label>
              <input
                id="email"
                name="email"
                type="email"
                required
                autoComplete="email"
                placeholder="admin@aegis.bank"
                className="w-full px-4 py-3 rounded-lg text-sm text-white
                           placeholder-gray-600 outline-none transition-all
                           focus:ring-2 focus:ring-cyan-500/30"
                style={{
                  background: "rgba(255,255,255,0.03)",
                  border: "1px solid rgba(255,255,255,0.06)",
                }}
              />
            </div>

            {/* Password */}
            <div>
              <label
                htmlFor="password"
                className="block text-xs font-medium text-gray-400 mb-2 uppercase tracking-wider"
              >
                Password
              </label>
              <input
                id="password"
                name="password"
                type="password"
                required
                autoComplete="current-password"
                placeholder="••••••••"
                className="w-full px-4 py-3 rounded-lg text-sm text-white
                           placeholder-gray-600 outline-none transition-all
                           focus:ring-2 focus:ring-cyan-500/30"
                style={{
                  background: "rgba(255,255,255,0.03)",
                  border: "1px solid rgba(255,255,255,0.06)",
                }}
              />
            </div>

            {/* Submit */}
            <button
              type="submit"
              className="w-full py-3 px-4 rounded-lg text-sm font-semibold
                         text-white transition-all duration-200
                         hover:shadow-lg hover:shadow-cyan-500/10
                         active:scale-[0.98]"
              style={{
                background:
                  "linear-gradient(135deg, rgba(6,182,212,0.8) 0%, rgba(139,92,246,0.8) 100%)",
              }}
            >
              Sign In
            </button>
          </form>

          {/* Demo credentials */}
          <div className="mt-6 pt-6 border-t border-white/[0.04]">
            <p className="text-xs text-gray-600 text-center mb-3">
              Demo Credentials
            </p>
            <div className="grid grid-cols-2 gap-3">
              <div className="px-3 py-2 rounded-lg bg-white/[0.02] border border-white/[0.04]">
                <p className="text-[10px] text-cyan-400/70 uppercase tracking-wider mb-1">
                  Admin
                </p>
                <p className="text-xs text-gray-400 font-mono">admin@aegis.bank</p>
                <p className="text-xs text-gray-500 font-mono">admin123</p>
              </div>
              <div className="px-3 py-2 rounded-lg bg-white/[0.02] border border-white/[0.04]">
                <p className="text-[10px] text-violet-400/70 uppercase tracking-wider mb-1">
                  CCO
                </p>
                <p className="text-xs text-gray-400 font-mono">cco@aegis.bank</p>
                <p className="text-xs text-gray-500 font-mono">cco123</p>
              </div>
            </div>
          </div>
        </div>

        {/* Footer */}
        <p className="mt-6 text-center text-xs text-gray-700">
          Protected by NextAuth.js v5 · Edge Runtime Middleware
        </p>
      </div>
    </div>
  );
}

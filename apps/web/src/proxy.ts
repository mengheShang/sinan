import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { COOKIE } from "@/lib/session";

// Optimistic check only: no cookie means no session, so go to the login page.
// The API verifies the cookie on every request, which is the real check.
export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (pathname === "/login" || request.cookies.has(COOKIE)) {
    return NextResponse.next();
  }
  const url = new URL("/login", request.url);
  return NextResponse.redirect(url);
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};

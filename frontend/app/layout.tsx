import type { Metadata } from "next";
import { Merriweather, Public_Sans } from "next/font/google";
import "./globals.css";

// The US Web Design System's typefaces: Public Sans for the interface, Merriweather
// for the filing text itself.
const sans = Public_Sans({ variable: "--font-public-sans", subsets: ["latin"] });
const serif = Merriweather({
  variable: "--font-merriweather",
  subsets: ["latin"],
  weight: ["400", "700"],
});

export const metadata: Metadata = {
  title: "Earnings Call Analyst",
  description:
    "Claims pulled from SEC 10-K filings, each shown next to the exact sentence it comes from.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${sans.variable} ${serif.variable} h-full antialiased`}>
      <body className="min-h-full bg-desk font-sans text-ink">{children}</body>
    </html>
  );
}

import "./globals.css";

export const metadata = {
  title: "AI Analytics Platform",
  description: "AI-powered data analysis and visualization platform",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}

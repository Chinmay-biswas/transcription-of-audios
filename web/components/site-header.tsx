import Link from "next/link";

const navigation = [
  { href: "/upload", label: "Process audio" },
  { href: "/meetings", label: "Meeting chat" },
  { href: "/analytics", label: "Analytics" }
];

export function SiteHeader() {
  return (
    <header className="site-header">
      <div className="shell header-content">
        <Link href="/" className="brand" aria-label="Meeting Intelligence home">
          <span className="brand-mark" aria-hidden="true">MI</span>
          <span>Meeting Intelligence</span>
        </Link>
        <nav className="site-nav" aria-label="Primary navigation">
          {navigation.map((item) => (
            <Link key={item.href} href={item.href}>
              {item.label}
            </Link>
          ))}
        </nav>
      </div>
    </header>
  );
}

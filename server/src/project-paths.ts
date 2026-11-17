/** Public paths can change without moving a project's Durable Object or ticket IDs. */
export function storedProject(slug: string): string {
  return slug === "beryl" ? "by" : slug;
}

export function dashboardRoot(project: string): string {
  return `/${project === "by" ? "beryl" : project}/`;
}

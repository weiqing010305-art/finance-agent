const params = new URLSearchParams(window.location.search);
const invitation = { tenantId: params.get("tenant_id") || "", token: params.get("token") || "" };
history.replaceState({}, "", "/invitation.html");

const form = document.getElementById("invitation-form");
const error = document.getElementById("invitation-error");
if (!invitation.tenantId || !invitation.token) {
  error.textContent = "邀请链接无效或不完整。";
  form.querySelector("button").disabled = true;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.textContent = "";
  const password = document.getElementById("invite-password").value;
  const confirmation = document.getElementById("invite-password-confirm").value;
  if (password !== confirmation) {
    error.textContent = "两次输入的密码不一致。";
    return;
  }
  try {
    const response = await fetch("/api/auth/invitations/accept", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tenant_id: invitation.tenantId, token: invitation.token, password }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "邀请接受失败");
    invitation.token = "";
    location.replace("/formal-console.html");
  } catch (caught) {
    error.textContent = caught.message;
  }
});

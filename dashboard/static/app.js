// Live preview for the message studio. Rendering happens on the server so the
// preview uses exactly the same code path as a real send.

function fieldValues() {
  const get = (id) => {
    const el = document.getElementById(id);
    if (!el) return "";
    return el.type === "checkbox" ? el.checked : el.value;
  };
  return {
    content: {
      body: get("body"),
      use_embed: get("use_embed"),
      embed_title: get("embed_title"),
      embed_color: get("embed_color"),
      image_url: get("image_url"),
      footer: get("footer"),
      button_label: get("button_label"),
      button_emoji: get("button_emoji"),
      button_mode: get("button_mode"),
      button_style: get("button_style"),
    },
    preview_guild_id: (document.getElementById("preview-guild") || {}).value || "",
  };
}

async function refreshPreview() {
  const payload = fieldValues();
  let data;
  try {
    const response = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    data = await response.json();
  } catch (err) {
    return;
  }

  const body = document.getElementById("p-body");
  const embed = document.getElementById("p-embed");
  const button = document.getElementById("p-button");
  document.getElementById("p-bot").textContent = data.bot_name || "Funnel Bot";

  if (data.use_embed) {
    body.textContent = "";
    embed.hidden = false;
    embed.style.borderLeftColor = data.embed_color || "#5865F2";
    document.getElementById("p-embed-title").textContent = data.embed_title || "";
    document.getElementById("p-embed-body").textContent = data.body || "";
    document.getElementById("p-footer").textContent = data.footer || "";
    const image = document.getElementById("p-image");
    image.hidden = !data.image_url;
    if (data.image_url) image.src = data.image_url;
  } else {
    embed.hidden = true;
    body.textContent = data.body || "";
  }

  const emoji = data.button_emoji ? data.button_emoji + " " : "";
  button.textContent = emoji + (data.button_label || "Join");
  button.href = data.invite_url || "#";

  // Discord's real button colours. A link button is always blurple-grey.
  const styleColours = {
    PRIMARY: "#5865f2",
    SECONDARY: "#4e5058",
    SUCCESS: "#248046",
    DANGER: "#da373c",
  };
  const interactive = data.button_mode === "INTERACTIVE";
  button.style.background = interactive
    ? styleColours[data.button_style] || styleColours.PRIMARY
    : "#4e5058";
  const buttons = Array.isArray(data.buttons) ? data.buttons : [{label:data.button_label,invite_url:data.invite_url}];
  const container=document.getElementById("p-buttons");
  if(container){
    container.querySelectorAll("[data-extra-button]").forEach(node=>node.remove());
    button.hidden=buttons.length===0;
    if(buttons.length){button.textContent=emoji+buttons[0].label;button.href=buttons[0].invite_url;}
    for(const item of buttons.slice(1)){
      const next=button.cloneNode(false);next.removeAttribute("id");next.dataset.extraButton="1";
      next.textContent=emoji+item.label;next.href=item.invite_url;container.appendChild(next);
    }
  }
  const note = document.getElementById("p-button-note");
  if (note) {
    note.textContent = interactive
      ? "Interactive button: the bot replies privately with the tracking invite."
      : "Link button: opens the tracking invite directly. Discord fixes its colour.";
  }

  const warn = document.getElementById("p-warn");
  if (data.unknown_variables && data.unknown_variables.length) {
    warn.hidden = false;
    warn.textContent =
      "These look like variables but are not recognised, so they will be sent as written: " +
      data.unknown_variables.map((v) => "{" + v + "}").join(", ");
  } else if(data.truncated_fields && data.truncated_fields.length) {
    warn.hidden=false;
    warn.textContent="Shortened to fit Discord: "+data.truncated_fields.join(", ");
  } else {
    warn.hidden = true;
  }
}

function startPreview() {
  const ids = ["body", "button_label", "button_emoji", "use_embed", "embed_title",
               "embed_color", "image_url", "footer", "preview-guild",
               "button_mode", "button_style"];
  let timer = null;
  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(refreshPreview, 180);
  };
  ids.forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener("input", schedule);
      el.addEventListener("change", schedule);
    }
  });

  document.querySelectorAll("[data-insert]").forEach((chip) => {
    chip.addEventListener("click", () => {
      const area = document.getElementById("body");
      const token = chip.dataset.insert;
      const start = area.selectionStart || area.value.length;
      area.value = area.value.slice(0, start) + token + area.value.slice(area.selectionEnd || start);
      area.focus();
      area.selectionStart = area.selectionEnd = start + token.length;
      refreshPreview();
    });
  });

  refreshPreview();
}

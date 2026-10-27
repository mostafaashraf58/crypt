fn main() {
    // Registering the app command enables Tauri's application ACL for it. Only
    // the main-window capability below grants access to the session connection.
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&["get_engine_connection"]),
        ),
    )
    .expect("Unable to prepare Axiom desktop permissions");
}

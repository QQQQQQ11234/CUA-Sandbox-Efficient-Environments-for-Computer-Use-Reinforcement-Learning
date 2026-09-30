import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit, urlunsplit

import httpx
from omegaconf import DictConfig, OmegaConf
from playwright.async_api import Playwright, async_playwright

from rl_web_agent.isolation import DBAgentSession, DBIsolationManager


class WebAgentEnv:
    _shared_playwright: ClassVar[Playwright | None] = None
    _shared_playwright_users: ClassVar[int] = 0
    _shared_playwright_lock: ClassVar[asyncio.Lock] = asyncio.Lock()
    _shared_playwright_browsers_path: ClassVar[str | None] = None

    def __init__(self, environment_config: DictConfig):
        self.config = environment_config
        self.context_manager = None
        self.browser = None
        self.context = None
        self.page = None  # Current active page
        # Note: pages are managed by self.context.pages
        self.uuid = environment_config.uuid if hasattr(environment_config, "uuid") else str(uuid.uuid4())
        self.logger = logging.getLogger(__name__)
        self.task_config: dict | None = None
        self.server_ips: dict[str, str] = {}  # Mapping of site name to server IP
        self.model_answer: str | None = None  # Model's final answer/response
        self.extra_headers: dict[str, str] = {}  # Host rewrite headers for proxy
        self.trace_file_path: str | None = None  # Path to the current trace file
        self.db_isolation_manager: DBIsolationManager | None = None
        self.db_agent_session: DBAgentSession | None = None
        # Per-task counters used for rollout timing breakdowns.
        self.rollout_metrics = {
            "env_time_s": 0.0,
            "env_steps": 0,
            "timeout_count": 0,
            "reward_time_s": 0.0,
        }
        self._playwright_acquired = False
        self.isolation_mode = self._resolve_isolation_mode()

    def _resolve_isolation_mode(self) -> str:
        if hasattr(self.config, "isolation") and getattr(self.config.isolation, "mode", None):
            return str(self.config.isolation.mode).strip().lower()
        return "db"

    @classmethod
    async def _ensure_playwright(cls, browsers_path: str | None = None) -> Playwright:
        """Ensure shared Playwright instance exists and return it"""
        async with cls._shared_playwright_lock:
            if cls._shared_playwright is None:
                if browsers_path:
                    resolved_path = str(Path(browsers_path).expanduser().resolve())
                    Path(resolved_path).mkdir(parents=True, exist_ok=True)
                    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = resolved_path
                    cls._shared_playwright_browsers_path = resolved_path
                cls._shared_playwright = await async_playwright().start()
            elif browsers_path:
                resolved_path = str(Path(browsers_path).expanduser().resolve())
                if resolved_path != cls._shared_playwright_browsers_path:
                    raise RuntimeError(
                        "Cannot change Playwright browser path while browsers are active: "
                        f"{cls._shared_playwright_browsers_path!r} -> {resolved_path!r}"
                    )
            cls._shared_playwright_users += 1
            return cls._shared_playwright

    @classmethod
    async def _cleanup_playwright(cls) -> None:
        """Cleanup shared Playwright instance if no more users"""
        async with cls._shared_playwright_lock:
            if cls._shared_playwright_users > 0:
                cls._shared_playwright_users -= 1
            if cls._shared_playwright_users == 0 and cls._shared_playwright is not None:
                await cls._shared_playwright.stop()
                cls._shared_playwright = None
                cls._shared_playwright_browsers_path = None

    async def _setup_tracing(self) -> None:
        """Setup Playwright tracing if enabled in config"""
        if not self.config.tracing.enabled:
            self.logger.debug("Tracing not enabled")
            return

        # Use trace output path directly as file path
        self.trace_file_path = self.config.tracing.output_path

        # Start tracing with configured options
        await self.context.tracing.start(
            screenshots=self.config.tracing.get("screenshots", True),
            snapshots=self.config.tracing.get("snapshots", True),
            sources=self.config.tracing.get("sources", True),
        )

        self.logger.info(f"Tracing started, will save to: {self.trace_file_path}")

    async def _stop_tracing(self) -> None:
        """Stop tracing and save trace file"""
        if self.trace_file_path and self.context:
            try:
                await self.context.tracing.stop(path=self.trace_file_path)
                self.logger.info(f"Trace saved to: {self.trace_file_path}")
            except Exception as e:
                self.logger.warning(f"Failed to save trace: {e}")
            finally:
                self.trace_file_path = None

    async def _start_recording(self) -> None:
        """Start screen recording using QuickRecorder"""
        try:
            self.logger.info("Starting screen recording setup...")

            # Create a new page for recording identification
            recording_page = await self.context.new_page()

            # Set the page title to the current UUID for window identification
            await recording_page.evaluate(
                f"""
                document.title = "{self.uuid}";
            """
            )

            # Give the page a moment to update the title
            await asyncio.sleep(2)

            # Use AppleScript to start QuickRecorder
            applescript = f"""
            tell application "QuickRecorder"
                activate
                record window titled "{self.uuid}" in application "Chromium"
            end tell
            """

            # Execute AppleScript
            import subprocess

            result = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True, timeout=10)

            if result.returncode == 0:
                self.logger.info(f"Successfully started recording for window with UUID: {self.uuid}")
            else:
                self.logger.error(f"Failed to start recording: {result.stderr}")

            await asyncio.sleep(2)

            # Close the recording setup page
            await recording_page.close()

        except Exception as e:
            self.logger.error(f"Error starting recording: {e}")
            # Don't raise the exception - recording failure shouldn't stop the task

    async def _get_tabs_info(self) -> list[dict]:
        """Get information about all open tabs"""
        tabs_info = []
        for i, page in enumerate(self.context.pages):
            tabs_info.append({"id": i, "title": await page.title(), "url": page.url, "is_active": page == self.page})
        return tabs_info

    async def _setup_db_isolation(self, required_sites: list[str]) -> None:
        if not hasattr(self.config, "db_isolation"):
            raise RuntimeError("DB isolation mode requires `environment.db_isolation` config")

        if self.db_isolation_manager is None:
            self.db_isolation_manager = DBIsolationManager(
                self.config.db_isolation,
                self.logger,
            )

        session = await asyncio.to_thread(
            self.db_isolation_manager.prepare_for_task,
            self.uuid,
            self.task_config,
        )
        self.db_agent_session = session
        self.extra_headers.update(session.headers)

        for site in required_sites:
            shared_host = session.shared_site_hosts.get(site)
            if shared_host:
                self.server_ips[site] = shared_host
            else:
                self.logger.warning(
                    f"DB isolation: no shared host mapping configured for site `{site}`; "
                    "this site will use its original URL without host rewrite."
                )

        self.logger.info(
            f"DB isolation active: agent_id={session.agent_id}, db_name={session.db_name}"
        )

    async def _install_db_route_header_injection(
        self, required_sites: list[str]
    ) -> None:
        if self.isolation_mode != "db" or not self.db_agent_session:
            return

        def authority(value: str) -> str:
            parsed = urlsplit(value if "://" in value else f"//{value}")
            return parsed.netloc

        target_by_authority: dict[str, str] = {}
        canonical_by_authority: dict[str, str] = {}
        for site in required_sites:
            if site not in self.config.sites or site not in self.server_ips:
                continue
            source = authority(str(self.config.sites[site]))
            target = authority(str(self.server_ips[site]))
            if source and target:
                target_by_authority[source] = target
                canonical_by_authority[source] = source
                # WebArena task manifests use the benchmark-local authorities
                # (for example ``shopping.local``), while older CUA-Sandbox
                # configs use the legacy ``metis.lti.cs.cmu.edu`` authorities.
                # Route interception happens before navigation/DNS, so these
                # aliases must be part of the reviewed mapping as well.
                local_aliases = {
                    "shopping": ("shopping.local",),
                    "shopping_admin": ("shopping-admin.local",),
                }
                for alias in local_aliases.get(site, ()):
                    target_by_authority[alias] = target
                    canonical_by_authority[alias] = source
                source_port = urlsplit(f"//{source}").port
                if source_port is not None:
                    target_by_authority[f"127.0.0.1:{source_port}"] = target
                    target_by_authority[f"localhost:{source_port}"] = target

        allowed_authorities = set(target_by_authority) | set(
            target_by_authority.values()
        )
        route_headers = dict(self.db_agent_session.headers)

        async def inject_route_header(route, request) -> None:
            parsed = urlsplit(request.url)
            if parsed.netloc not in allowed_authorities:
                await route.continue_()
                return
            headers = dict(request.headers)
            headers.update(route_headers)
            target = target_by_authority.get(parsed.netloc)
            if target is None:
                await route.continue_(headers=headers)
                return

            # Keep the benchmark-visible URL/cookies scoped to the original
            # WebArena authority while sending bytes to the shared app.  This
            # is the in-process equivalent of the legacy rewrite proxy and is
            # restricted to reviewed site mappings plus a signed DB route.
            rewritten = urlunsplit(
                (parsed.scheme, target, parsed.path, parsed.query, parsed.fragment)
            )
            # The shared Magento frontend selects its virtual host before it
            # evaluates the signed route header. Preserve the configured
            # benchmark authority after rewriting the network destination.
            canonical_authority = canonical_by_authority.get(parsed.netloc)
            if canonical_authority:
                headers["host"] = canonical_authority
            await route.continue_(url=rewritten, headers=headers)

        await self.context.route("**/*", inject_route_header)
        self.logger.info(
            "Trusted DB request rewrite installed: %s",
            target_by_authority,
        )

    async def _wait_for_containers_online(self) -> None:
        """Wait for all launched containers to be online using HTTP HEAD requests with retry logic"""
        self.logger.info("Waiting for containers to come online...")

        # Get timeout from config (convert from milliseconds to seconds)
        timeout_seconds = self.config.browser.timeouts.container_health_check / 1000
        retry_interval = 2.0  # Wait 2 seconds between retries

        # Set up proxy if enabled
        proxy = None
        if self.config.proxy.enabled:
            proxy = self.config.proxy.server
            self.logger.info(f"Using proxy for health checks: {self.config.proxy.server}")

        # Track which sites still need to come online
        pending_sites = {}
        for site_name, ip_address in self.server_ips.items():
            if site_name not in self.config.server_port_map:
                self.logger.warning(
                    f"No server_port_map found for site `{site_name}`; "
                    "skip health check for this site."
                )
                continue
            parsed_target = urlsplit(
                str(ip_address)
                if "://" in str(ip_address)
                else f"//{ip_address}"
            )
            if parsed_target.port is not None:
                health_target = str(ip_address)
            else:
                health_target = (
                    f"{ip_address}:{self.config.server_port_map[site_name]}"
                )
            pending_sites[site_name] = health_target

        if not pending_sites:
            self.logger.info("No containers to health check (all using placeholder IPs)")
            return

        # Track start time for overall timeout
        start_time = asyncio.get_event_loop().time()

        # Create httpx client with per-request timeout
        async with httpx.AsyncClient(
            timeout=10.0,  # Shorter per-request timeout
            proxy=proxy,  # Use 'proxy' not 'proxies' for httpx
            follow_redirects=True,
        ) as client:
            while pending_sites and (asyncio.get_event_loop().time() - start_time) < timeout_seconds:
                # Try each pending site
                sites_to_remove = []

                for site_name, health_target in pending_sites.items():
                    try:
                        # Construct health check URL
                        health_url = (
                            health_target
                            if "://" in health_target
                            else f"http://{health_target}"
                        )

                        # Use HEAD request to check if port is open and responding
                        response = await client.head(health_url, follow_redirects=False)

                        if response.status_code < 400:  # Accept any 2xx or 3xx status
                            self.logger.info(f"✅ {site_name} is now online (status: {response.status_code})")
                            sites_to_remove.append(site_name)
                        else:
                            self.logger.debug(f"⏳ {site_name} returned status {response.status_code}, retrying...")

                    except (httpx.TimeoutException, httpx.ConnectError, httpx.RequestError):
                        # These are expected during startup, just continue retrying
                        self.logger.debug(f"⏳ {site_name} not ready yet, retrying...")
                    except Exception as e:
                        self.logger.warning(f"⚠️ Unexpected error for {site_name}: {e}, retrying...")

                # Remove sites that are now online
                for site_name in sites_to_remove:
                    del pending_sites[site_name]

                # If all sites are online, we're done
                if not pending_sites:
                    break

                # Wait before next retry attempt
                self.logger.info(f"⏳ Waiting for {len(pending_sites)} containers: {list(pending_sites.keys())}")
                await asyncio.sleep(retry_interval)

        # Check if we timed out
        if pending_sites:
            elapsed = asyncio.get_event_loop().time() - start_time
            self.logger.error(f"❌ Timeout after {elapsed:.1f}s waiting for containers: {list(pending_sites.keys())}")
            self.logger.warning("Proceeding with setup despite some containers not being ready...")
        else:
            elapsed = asyncio.get_event_loop().time() - start_time
            self.logger.info(f"✅ All containers online after {elapsed:.1f}s!")

    async def login_to_site(self, site_name: str) -> None:
        """Login to a specific site using hardcoded login logic"""
        if not hasattr(self.config, "accounts") or site_name not in self.config.accounts:
            self.logger.warning(f"No account configured for site: {site_name}")
            return

        account = self.config.accounts[site_name]
        username = account["username"]
        password = account["password"]

        # Create a dedicated login page
        login_page = await self.context.new_page()

        try:
            if site_name == "shopping":
                login_url = f"http://{self.config.sites[site_name]}/customer/account/login/"
                await login_page.goto(login_url, wait_until="networkidle", timeout=self.config.browser.timeouts.page_load_networkidle)
                await login_page.get_by_label("Email", exact=True).fill(username)
                await login_page.get_by_label("Password", exact=True).fill(password)
                await asyncio.sleep(2)  # Additional wait for login to complete
                await login_page.get_by_role("button", name="Sign In").click()
                # Wait for navigation after login
                await login_page.wait_for_load_state("networkidle", timeout=self.config.browser.timeouts.page_load_networkidle)
                await asyncio.sleep(2)  # Additional wait for login to complete

            elif site_name == "reddit":
                login_url = f"http://{self.config.sites[site_name]}/login"
                await login_page.goto(login_url, wait_until="networkidle", timeout=self.config.browser.timeouts.page_load_networkidle)
                await login_page.get_by_label("Username").fill(username)
                await login_page.get_by_label("Password").fill(password)
                await login_page.get_by_role("button", name="Log in").click()
                # Wait for navigation after login
                await login_page.wait_for_load_state("networkidle", timeout=self.config.browser.timeouts.page_load_networkidle)
                await asyncio.sleep(2)  # Additional wait for login to complete

            elif site_name == "shopping_admin":
                login_url = f"http://{self.config.sites[site_name]}/admin/dashboard"
                await login_page.goto(login_url, wait_until="networkidle", timeout=self.config.browser.timeouts.page_load_networkidle)
                await login_page.get_by_placeholder("user name").fill(username)
                await login_page.get_by_placeholder("password").fill(password)
                await login_page.get_by_role("button", name="Sign in").click()
                # Wait for navigation after login
                await login_page.wait_for_load_state("networkidle", timeout=self.config.browser.timeouts.page_load_networkidle)
                await asyncio.sleep(2)  # Additional wait for login to complete

            elif site_name == "gitlab":
                login_url = f"http://{self.config.sites[site_name]}/users/sign_in"
                await login_page.goto(login_url, wait_until="networkidle", timeout=self.config.browser.timeouts.page_load_networkidle)
                await login_page.get_by_test_id("username-field").click()
                await login_page.get_by_test_id("username-field").fill(username)
                await login_page.get_by_test_id("username-field").press("Tab")
                await login_page.get_by_test_id("password-field").fill(password)
                await login_page.get_by_test_id("sign-in-button").click()
                # Wait for navigation after login
                await login_page.wait_for_load_state("networkidle", timeout=self.config.browser.timeouts.page_load_networkidle)
                await asyncio.sleep(2)  # Additional wait for login to complete

            else:
                self.logger.warning(f"No login logic implemented for site: {site_name}")
                return

            self.logger.info(f"Successfully logged into {site_name}")

        except Exception as e:
            self.logger.error(f"Failed to login to {site_name}: {e}")
            raise
        finally:
            # Close the dedicated login page
            await login_page.close()

    async def ensure_logged_in(self, required_sites: list[str]) -> None:
        """Ensure user is logged into all required sites"""
        for site_name in required_sites:
            if site_name in self.config.sites:
                await self.login_to_site(site_name)
            else:
                self.logger.warning(f"Site not configured: {site_name}")

    def _browser_proxy_server(self) -> str | None:
        """Return the legacy proxy only for container-based isolation."""
        if self.isolation_mode == "db":
            return None
        if bool(self.config.proxy.enabled):
            return str(self.config.proxy.server)
        return None

    async def setup(self, task_config: dict | None = None):
        """Initialize the browser environment with configuration"""
        self.task_config = task_config
        browsers_path = self.config.browser.get("browsers_path")
        self.context_manager = await self._ensure_playwright(browsers_path)
        self._playwright_acquired = True
        self.server_ips = {}
        self.extra_headers = {}
        self.db_agent_session = None

        required_sites = self.task_config["sites"] if self.task_config and "sites" in self.task_config else []
        if required_sites:
            if self.isolation_mode == "db":
                await self._setup_db_isolation(required_sites)
            elif self.isolation_mode == "direct":
                # Use an already-running official WebArena Docker service.
                # Direct mode uses an already-running official WebArena service.
                shared_hosts = OmegaConf.to_container(
                    self.config.db_isolation.shared_site_hosts, resolve=True
                )
                self.server_ips = {
                    site: str(shared_hosts[site])
                    for site in required_sites
                    if site in shared_hosts
                }
            else:
                raise ValueError(
                    f"Unsupported isolation mode `{self.isolation_mode}`; expected `db` or `direct`."
                )
        else:
            self.logger.warning("No sites specified in task config")

        # Get launch options from config and convert to dict
        launch_options = OmegaConf.to_container(self.config.browser.launch_options, resolve=True)

        # Add cache directory if configured
        if hasattr(self.config.browser, "cache_dir") and self.config.browser.cache_dir:
            # Use absolute path for cache directory
            cache_dir = Path(self.config.browser.cache_dir).resolve()
            cache_dir.mkdir(parents=True, exist_ok=True)  # Ensure directory exists
            cache_arg = f"--disk-cache-dir={cache_dir}"
            launch_options["args"] = launch_options.get("args", []) + [cache_arg]
            self.logger.info(f"Browser cache configured: {cache_arg}")

        # DB isolation installs a reviewed, signed Playwright route directly to
        # the shared application.  Sending those requests through the legacy
        # Direct-mode proxy rewriting bypasses that route and makes an unavailable
        # localhost:8080 fail every task before the first observation.
        browser_proxy = self._browser_proxy_server()
        if browser_proxy:
            launch_options["proxy"] = {"server": browser_proxy}
        elif self.config.proxy.enabled:
            self.logger.info("DB isolation bypasses the legacy browser proxy")

        # Get context options from config and convert to dict
        context_options = OmegaConf.to_container(self.config.browser.context_options, resolve=True)

        storage_state = (
            self.task_config.get("storage_state")
            if self.task_config is not None
            else None
        )
        if storage_state:
            storage_path = Path(str(storage_state)).expanduser().resolve()
            if not storage_path.is_file():
                raise FileNotFoundError(
                    f"task browser storage state does not exist: {storage_path}"
                )
            context_options["storage_state"] = str(storage_path)

        # Add host rewrite headers for each site
        extra_headers = dict(self.extra_headers)
        rewrite_mappings = []
        for site_name, hostname in self.config.sites.items():
            if site_name in self.server_ips:
                server_target = self.server_ips[site_name]
                if ":" in str(server_target):
                    rewrite_mapping = f"{hostname}={server_target}"
                else:
                    if site_name not in self.config.server_port_map:
                        self.logger.warning(
                            f"No server_port_map found for site `{site_name}`; "
                            "skip rewrite mapping for this site."
                        )
                        continue
                    rewrite_mapping = (
                        f"{hostname}={server_target}:{self.config.server_port_map[site_name]}"
                    )
                rewrite_mappings.append(rewrite_mapping)
                self.logger.info(f"Added host rewrite for {site_name}: {rewrite_mapping}")

        if rewrite_mappings:
            # Use the first mapping as primary header (most common case is single site)
            extra_headers["x-target-host-rewrite"] = rewrite_mappings[0]
            # For multiple sites, we may need additional headers but this handles the common case

        # Store extra headers for later use in evaluation
        self.extra_headers = extra_headers

        browser_headers = dict(extra_headers)
        if self.db_agent_session:
            for header_name in self.db_agent_session.headers:
                browser_headers.pop(header_name, None)
        if browser_headers:
            context_options["extra_http_headers"] = browser_headers

        # Check if user_data_dir is specified - use launch_persistent_context if so
        user_data_dir = None
        if hasattr(self.config.browser, "user_data_dir") and self.config.browser.user_data_dir:
            user_data_dir = self.config.browser.user_data_dir

        if user_data_dir:
            # Use launch_persistent_context for user data directory
            # Remove --disk-cache-dir from args since persistent context manages its own cache
            persistent_options = {**launch_options, **context_options}
            if "args" in persistent_options:
                persistent_options["args"] = [arg for arg in persistent_options["args"] if not arg.startswith("--disk-cache-dir")]

            self.context = await self.context_manager.chromium.launch_persistent_context(user_data_dir, **persistent_options)
            self.browser = self.context.browser
            self.logger.info(f"Using persistent context with cache in user data dir: {user_data_dir}")
        else:
            # Regular launch without persistent context
            self.browser = await self.context_manager.chromium.launch(**launch_options)
            self.context = await self.browser.new_context(**context_options)

        await self._install_db_route_header_injection(required_sites)

        # Start tracing if enabled
        await self._setup_tracing()

        # Set default timeout for all locator actions
        self.context.set_default_timeout(self.config.browser.timeouts.default)

        # Add init script if it exists
        init_script_path = Path(self.config.init_script_path)
        if init_script_path.exists():
            with open(init_script_path) as f:
                await self.context.add_init_script(f.read())
        else:
            self.logger.warning(f"Init script not found: {init_script_path}")

        # Create initial page (or use existing one from persistent context)
        if self.context.pages:
            # Use existing page from persistent context
            self.page = self.context.pages[0]
        else:
            # Create new page for regular context
            self.page = await self.context.new_page()

        # Handle authentication before navigating to start_url
        if self.task_config and "sites" in self.task_config and not storage_state:
            required_sites = self.task_config["sites"]
            await self.ensure_logged_in(required_sites)
        elif storage_state:
            self.logger.info("Loaded task-provided browser authentication state")

        # Start recording if enabled
        if self.config.recording.enabled:
            await self._start_recording()

        # Navigate to start URL from task config
        if self.task_config and "start_url" in self.task_config:
            await self.page.goto(self.task_config["start_url"], wait_until="domcontentloaded", timeout=self.config.browser.timeouts.page_load_domcontent)
        else:
            self.logger.warning("No start_url specified in task config")
        return await self.observation(skip_evaluation=True)

    async def new_tab(self, url: str | None = None) -> int:
        """Create a new tab and optionally navigate to URL. Returns tab ID."""
        page = await self.context.new_page()
        if url:
            await page.goto(url, wait_until="domcontentloaded")
        self.page = page  # Make new tab active
        return len(self.context.pages) - 1

    async def switch_tab(self, tab_id: int) -> None:
        """Switch to a different tab by ID"""
        if 0 <= tab_id < len(self.context.pages):
            self.page = self.context.pages[tab_id]
            await self.page.bring_to_front()
        else:
            raise ValueError(f"Invalid tab ID: {tab_id}")

    async def close_tab(self, tab_id: int) -> None:
        """Close a tab by ID"""
        if 0 <= tab_id < len(self.context.pages):
            page = self.context.pages[tab_id]
            await page.close()
            # If we closed the active tab, switch to the currently activated tab from context
            if page == self.page and self.context.pages:
                # Find the currently active/focused tab in the context
                for p in self.context.pages:
                    try:
                        if await p.evaluate("document.hasFocus()"):
                            self.page = p
                            break
                    except Exception:
                        continue
                else:
                    # Fallback to last tab if no focused tab found
                    self.page = self.context.pages[-1]

                # Ensure the new active page is brought to front
                await self.page.bring_to_front()
        else:
            raise ValueError(f"Invalid tab ID: {tab_id}")

    async def reset(self):
        """Reset the environment to initial state"""
        if self.isolation_mode == "db":
            if not self.db_isolation_manager or not self.db_agent_session:
                raise RuntimeError("database reset requires an active DB-isolation session")
            await asyncio.to_thread(
                self.db_isolation_manager.reset,
                self.db_agent_session,
            )

        # Close all tabs
        for page in self.context.pages:
            await page.close()
        await self.context.clear_cookies()
        self.page = await self.context.new_page()

        if self.task_config and "sites" in self.task_config:
            await self.ensure_logged_in(self.task_config["sites"])

        # Return to start URL from task config
        if self.task_config and "start_url" in self.task_config:
            await self.page.goto(self.task_config["start_url"], wait_until="domcontentloaded")
        else:
            self.logger.warning("No start_url specified in task config")
        return await self.observation()

    async def step(self, action: str):
        """
        Execute an action in the environment using JSON string format and return the next observation.

        Args:
            action: JSON string describing the action to execute

        Returns:
            dict: The observation after executing the action (same format as observation() method)

        Examples:
            obs = await env.step('{"action": "click", "target": "login_button"}')
            obs = await env.step('{"action": "type", "target": "username", "text": "john_doe", "enter": true}')
            obs = await env.step('{"action": "select", "target": "country", "value": "US"}')
            obs = await env.step('{"action": "goto_url", "url": "https://example.com"}')
            obs = await env.step('{"action": "back"}')
            obs = await env.step('{"action": "new_tab", "url": "https://example.com"}')
            obs = await env.step('{"action": "switch_tab", "tab_id": 1}')
            obs = await env.step('{"action": "close_tab", "tab_id": 1}')
            obs = await env.step('{"action": "terminate", "answer": "The product costs $29.99"}')
        """
        import json
        _step_started = asyncio.get_running_loop().time()
        observation = None

        try:
            action_data = json.loads(action)
            action_name = action_data.get("action")

            if action_name == "click":
                await self.click(action_data["target"])

            elif action_name == "type":
                text = action_data["text"]
                target = action_data["target"]
                press_enter = action_data.get("enter", False)
                await self.type(target, text, press_enter)

            elif action_name == "hover":
                await self.hover(action_data["target"])

            elif action_name == "select":
                await self.select(action_data["target"], action_data["value"])

            elif action_name == "clear":
                await self.clear(action_data["target"])

            elif action_name == "key_press":
                key = action_data["key"]
                target = action_data.get("target")
                await self.key_press(key, target)

            elif action_name == "goto_url":
                await self.goto_url(action_data["url"])

            elif action_name == "back":
                await self.back()

            elif action_name == "forward":
                await self.forward()

            elif action_name == "refresh":
                await self.refresh()

            elif action_name == "new_tab":
                url = action_data.get("url")
                await self.new_tab(url)

            elif action_name == "switch_tab":
                tab_id = action_data["tab_id"]
                await self.switch_tab(tab_id)

            elif action_name == "close_tab":
                tab_id = action_data["tab_id"]
                await self.close_tab(tab_id)

            elif action_name == "terminate":
                answer = action_data.get("answer", "")
                await self.terminate(answer)

            else:
                self.logger.error(f"Unknown action: {action_name}")
                raise ValueError(f"Unknown action: {action_name}")

            # Sleep after action if configured
            if self.config.browser.sleep_after_action > 0:
                await asyncio.sleep(self.config.browser.sleep_after_action)

            # Return the next observation after executing the action
            observation = await self.observation()
            observation["error"] = None
            return observation

        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid JSON action format: {action}")
            observation = await self.observation()
            observation["error"] = f"Invalid JSON action format: {e}"
            return observation
        except KeyError as e:
            self.logger.error(f"Missing required parameter in action: {e}")
            observation = await self.observation()
            observation["error"] = f"Missing required parameter in action: {e}"
            return observation
        except Exception as e:
            self.logger.error(f"Error executing action: {action}, error: {e}")
            observation = await self.observation()
            observation["error"] = f"Error executing action: {e}"
            return observation
        finally:
            elapsed = asyncio.get_running_loop().time() - _step_started
            self.rollout_metrics["env_time_s"] += elapsed
            self.rollout_metrics["env_steps"] += 1
            error_text = str((observation or {}).get("error") or "")
            if "timeout" in error_text.lower() or "timed out" in error_text.lower():
                self.rollout_metrics["timeout_count"] += 1

    # ===================================================================
    # ACTION METHODS
    # ===================================================================

    async def click(self, semantic_id: str) -> None:
        """
        Click on an element identified by its semantic ID.

        Args:
            semantic_id: The data-semantic-id of the element to click

        Example:
            await env.click("login_button")
            await env.click("menu.settings")
        """
        selector = f'[data-semantic-id="{semantic_id}"]'
        element = self.page.locator(selector)

        # Short timeout scroll - fail fast on hallucinated elements
        # Since we provide full page content, elements should exist
        await element.scroll_into_view_if_needed(timeout=500)
        await element.click(force=True)
        self.logger.info(f"Clicked element: {semantic_id}")

    async def type(self, semantic_id: str, text: str, press_enter: bool = False) -> None:
        """
        Type text into an input element.

        Args:
            semantic_id: The data-semantic-id of the input element
            text: Text to type
            press_enter: Whether to press Enter after typing

        Example:
            await env.type("search_input", "hello world")
            await env.type("username", "john_doe", press_enter=True)
        """
        selector = f'[data-semantic-id="{semantic_id}"]'
        element = self.page.locator(selector)

        # Short timeout scroll - fail fast on hallucinated elements
        await element.scroll_into_view_if_needed(timeout=500)
        await element.fill(text, force=True)  # Clear and type

        if press_enter:
            # Locator.press() has no ``force`` option in Playwright. The
            # element was just filled (and is therefore focused), so a normal
            # key press matches WebArena's keyboard action semantics.
            await element.press("Enter")

        self.logger.info(f"Typed '{text}' into element: {semantic_id}")

    async def hover(self, semantic_id: str) -> None:
        """
        Hover over an element to trigger tooltips or dropdown menus.

        Args:
            semantic_id: The data-semantic-id of the element to hover over

        Example:
            await env.hover("menu_item")
            await env.hover("tooltip_trigger")
        """
        selector = f'[data-semantic-id="{semantic_id}"]'
        element = self.page.locator(selector)

        # Short timeout scroll - fail fast on hallucinated elements
        await element.scroll_into_view_if_needed(timeout=500)
        await element.hover(force=True)
        self.logger.info(f"Hovered over element: {semantic_id}")

    async def select(self, semantic_id: str, value: str) -> None:
        """
        Select an option from a dropdown/select element.

        Args:
            semantic_id: The data-semantic-id of the select element
            value: The value of the option to select

        Example:
            await env.select("country_dropdown", "USA")
            await env.select("language_select", "en")
        """
        selector = f'[data-semantic-id="{semantic_id}"]'
        element = self.page.locator(selector)

        # Short timeout scroll - fail fast on hallucinated elements
        await element.scroll_into_view_if_needed(timeout=500)
        await element.select_option(value, force=True)
        self.logger.info(f"Selected '{value}' in element: {semantic_id}")

    async def clear(self, semantic_id: str) -> None:
        """
        Clear the content of an input element.

        Args:
            semantic_id: The data-semantic-id of the input element to clear

        Example:
            await env.clear("search_input")
            await env.clear("comment_textarea")
        """
        selector = f'[data-semantic-id="{semantic_id}"]'
        element = self.page.locator(selector)

        # Short timeout scroll - fail fast on hallucinated elements
        await element.scroll_into_view_if_needed(timeout=500)
        await element.clear(force=True)
        self.logger.info(f"Cleared element: {semantic_id}")

    async def key_press(self, key: str, semantic_id: str | None = None) -> None:
        """
        Press a keyboard key, optionally on a specific element.

        Args:
            key: Key to press (e.g., "Enter", "Escape", "Tab", "ArrowDown")
            semantic_id: Optional element to focus before pressing key

        Example:
            await env.key_press("Escape")  # Press Escape globally
            await env.key_press("Enter", "search_input")  # Press Enter on search input
            await env.key_press("ArrowDown", "dropdown")  # Navigate dropdown
        """
        if semantic_id:
            selector = f'[data-semantic-id="{semantic_id}"]'
            element = self.page.locator(selector)
            # Short timeout scroll - fail fast on hallucinated elements
            await element.scroll_into_view_if_needed(timeout=500)
            await element.press(key)
            self.logger.info(f"Pressed '{key}' on element: {semantic_id}")
        else:
            await self.page.keyboard.press(key)
            self.logger.info(f"Pressed '{key}' globally")

    # ===================================================================
    # NAVIGATION ACTIONS
    # ===================================================================

    async def goto_url(self, url: str) -> None:
        """
        Navigate to a specific URL in the current tab.

        Args:
            url: URL to navigate to

        Example:
            await env.goto_url("https://google.com")
            await env.goto_url("http://localhost:3000/login")
        """
        await self.page.goto(url, wait_until="domcontentloaded")
        self.logger.info(f"Navigated to: {url}")

    async def back(self) -> None:
        """
        Navigate back in browser history.

        Example:
            await env.back()
        """
        await self.page.go_back(wait_until="domcontentloaded")
        self.logger.info("Navigated back")

    async def forward(self) -> None:
        """
        Navigate forward in browser history.

        Example:
            await env.forward()
        """
        await self.page.go_forward(wait_until="domcontentloaded")
        self.logger.info("Navigated forward")

    async def refresh(self) -> None:
        """
        Refresh/reload the current page.

        Example:
            await env.refresh()
        """
        await self.page.reload(wait_until="domcontentloaded")
        self.logger.info("Page refreshed")

    async def terminate(self, answer: str = "") -> None:
        """
        Terminate the task with an optional answer.

        Args:
            answer: The model's final answer/response for the task

        Example:
            await env.terminate("The product costs $29.99")
            await env.terminate()  # Terminate without answer
        """
        self.model_answer = answer
        if answer:
            self.logger.info(f"Task terminated with answer: {answer}")
        else:
            self.logger.info("Task terminated without answer")

    async def _wait_for_custom_network_idle(self, timeout_ms: int = 10000, idle_time_ms: int = 500) -> None:
        """
        Custom network idle detection that works with XHR/fetch requests.
        Uses async JavaScript Promise-based waiting for better performance.
        """
        self.logger.info(f"Waiting for custom network idle (timeout: {timeout_ms}ms, idle: {idle_time_ms}ms)")

        try:
            # Add Python-side timeout as a safety net
            timeout_future = asyncio.create_task(asyncio.sleep(timeout_ms / 1000))
            evaluate_future = asyncio.create_task(
                self.page.evaluate(
                    """
                async ([idleTimeMs, timeoutMs]) => {
                    if (typeof window.__networkActivity === 'undefined') {
                        console.log('Network activity tracker not available');
                        return true; // Fallback if tracker not available
                    }

                    console.log('Starting network idle wait...');
                    try {
                        const isIdle = await window.__networkActivity.waitForIdle(idleTimeMs, timeoutMs);
                        console.log('Network idle wait completed:', isIdle);
                        return isIdle;
                    } catch (error) {
                        console.warn('Network idle wait error:', error);
                        return false;
                    }
                }
            """,
                    [idle_time_ms, timeout_ms],
                )
            )

            # Race between evaluation and timeout
            done, pending = await asyncio.wait([evaluate_future, timeout_future], return_when=asyncio.FIRST_COMPLETED)

            # Cancel pending tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            if evaluate_future in done:
                result = await evaluate_future
                if result:
                    self.logger.info("Custom network idle detected")
                else:
                    self.logger.warning(f"Custom network idle timeout after {timeout_ms}ms")
            else:
                self.logger.warning("Custom network idle detection timed out on Python side")

        except Exception as e:
            self.logger.warning(f"Custom network idle check failed: {e}")
            # Fallback to old polling method
            await self._wait_for_custom_network_idle_fallback(timeout_ms, idle_time_ms)

    async def _wait_for_custom_network_idle_fallback(self, timeout_ms: int = 10000, idle_time_ms: int = 500) -> None:
        """
        Fallback polling-based network idle detection.
        """
        start_time = asyncio.get_event_loop().time()
        timeout_seconds = timeout_ms / 1000

        self.logger.info("Using fallback network idle detection")

        while True:
            try:
                # Check if our network tracker is available and if network is idle
                is_idle = await self.page.evaluate(
                    """
                    (idleTimeMs) => {
                        if (typeof window.__networkActivity === 'undefined') {
                            return true; // Fallback if tracker not available
                        }
                        return window.__networkActivity.isIdle(idleTimeMs);
                    }
                """,
                    idle_time_ms,
                )

                if is_idle:
                    self.logger.info("Custom network idle detected (fallback)")
                    break

                # Check timeout
                if (asyncio.get_event_loop().time() - start_time) >= timeout_seconds:
                    self.logger.warning(f"Custom network idle timeout after {timeout_ms}ms (fallback)")
                    break

                # Wait a bit before checking again
                await asyncio.sleep(0.1)

            except Exception as e:
                self.logger.warning(f"Custom network idle fallback check failed: {e}")
                break

    async def observation(self, skip_evaluation: bool = True):
        """Get parsed page content using the parser script"""
        parser_script_path = Path(self.config.parser_script_path)
        content = {}

        # Wait for page to be fully loaded and stable
        try:
            self.logger.info("Waiting for page to be fully loaded and stable")
            await self.page.wait_for_load_state("domcontentloaded", timeout=self.config.browser.timeouts.page_load_domcontent)

            # Use both original networkidle (for page loads) and custom detection (for XHR/fetch)
            try:
                # First wait for Playwright's networkidle (handles initial page loads well)
                await self.page.wait_for_load_state("networkidle", timeout=self.config.browser.timeouts.page_load_networkidle)  # Shorter timeout
                self.logger.info("Playwright networkidle detected")
            except Exception as e:
                self.logger.info(f"Playwright networkidle timeout (normal): {e}")

            # Then wait for custom network idle detection (handles XHR/fetch after interactions)
            await self._wait_for_custom_network_idle(timeout_ms=self.config.browser.timeouts.page_load_networkidle, idle_time_ms=self.config.browser.timeouts.custom_network_idle)

            self.logger.info("Page loaded and stable")
        except Exception as e:
            self.logger.warning(f"Page load wait timeout: {e}")

        # Additional safety check - wait for body element
        try:
            await self.page.wait_for_selector("body", timeout=self.config.browser.timeouts.element_wait)
        except Exception as e:
            self.logger.warning(f"Body element not found: {e}")

        with open(parser_script_path) as f:
            parser_code = f.read()
        try:
            content = await self.page.evaluate(parser_code)
        except Exception as e:
            self.logger.error(f"Parser script failed: {e}")
            # Fallback to basic HTML content
            content = {"html": await self.page.content()}
        print("RAW HTML LENGTH", len(await self.page.content()))
        print("PARSED HTML LENGTH", len(content["html"]))
        print("-" * 100)

        # Add tabs information to the observation
        content["tabs"] = await self._get_tabs_info()

        # Add model answer if available
        content["model_answer"] = self.model_answer

        # Add evaluation information
        if not skip_evaluation and self.task_config and "eval" in self.task_config and self.config.get("evaluation", {}).get("enabled", True):
            score = await self.evaluate_task()
            content["score"] = score

            content["terminated"] = self.model_answer is not None
        else:
            content["score"] = 0.0
            content["terminated"] = self.model_answer is not None

        return content

    async def evaluate_task(self) -> float:
        """
        Evaluate current task using self.task_config.

        Returns:
            float: Score between 0.0 and 1.0 indicating task success

        Raises:
            ValueError: If task_config is not set or evaluation fails
            ImportError: If WebArena evaluation modules are not available
        """
        if self.task_config is None:
            raise ValueError("task_config must be set before evaluation")

        if (
            self.isolation_mode == "db"
            and self.db_isolation_manager
            and self.db_agent_session
        ):
            await asyncio.to_thread(
                self.db_isolation_manager.wait_for_background_jobs,
                self.db_agent_session,
            )

        # Import our simplified evaluator (no WebArena dependencies)
        from rl_web_agent.evaluator import evaluate_task

        # Run evaluation using our simplified evaluator
        # Pass individual parameters directly
        _reward_started = asyncio.get_running_loop().time()
        try:
            score = await evaluate_task(
                answer=self.model_answer or "",
                page=self.page,
                task_config=self.task_config,
                env_config=self.config,  # This has accounts, sites, etc.
                extra_headers=self.extra_headers,
            )
        finally:
            self.rollout_metrics["reward_time_s"] += (
                asyncio.get_running_loop().time() - _reward_started
            )

        self.logger.info(f"Task evaluation score: {score}")
        return score

    async def close(self):
        """Clean up and close the browser"""
        # Stop tracing if active
        await self._stop_tracing()

        # Stop all browser traffic before removing routes or isolated state.
        if self.context is not None:
            try:
                await self.context.close()
            except Exception as e:
                self.logger.warning(f"Failed to close browser context: {e}")
            finally:
                self.context = None
                self.page = None

        if self.browser is not None:
            try:
                if self.browser.is_connected():
                    await self.browser.close()
            except Exception as e:
                self.logger.warning(f"Failed to close browser: {e}")
            finally:
                self.browser = None

        # Clean up per-agent database in DB isolation mode
        if self.isolation_mode == "db" and self.db_isolation_manager and self.db_agent_session:
            try:
                await asyncio.to_thread(
                    self.db_isolation_manager.cleanup,
                    self.db_agent_session,
                )
            except Exception as e:
                self.logger.error(f"Error during DB isolation cleanup: {e}")
            finally:
                self.db_agent_session = None

        if self._playwright_acquired:
            await self._cleanup_playwright()
            self._playwright_acquired = False
        self.context_manager = None

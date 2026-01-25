"""
Natural Language handler for Cliara (Phase 2).
Currently stubbed - will integrate LLM in Phase 2.
"""

from typing import List, Tuple, Optional
from cliara.safety import SafetyChecker, DangerLevel


class NLHandler:
    """Handles natural language to command conversion."""
    
    def __init__(self, safety_checker: SafetyChecker):
        """
        Initialize NL handler.
        
        Args:
            safety_checker: Safety checker instance
        """
        self.safety = safety_checker
        self.llm_enabled = False  # Phase 2
    
    def process_query(self, query: str, context: Optional[dict] = None) -> Tuple[List[str], str, DangerLevel]:
        """
        Convert natural language query to commands.
        
        Args:
            query: Natural language query
            context: Optional context (cwd, os, shell, etc.)
        
        Returns:
            Tuple of (commands, explanation, danger_level)
        """
        # Phase 2: This will call LLM API
        # For now, return stub responses for common queries
        
        if not self.llm_enabled:
            return self._stub_response(query)
        
        # Phase 2 implementation:
        # 1. Prepare context
        # 2. Call LLM API with structured prompt
        # 3. Parse response (expecting JSON with commands, explanation, risks)
        # 4. Safety check the commands
        # 5. Return results
        
        return [], "LLM not configured", DangerLevel.SAFE
    
    def _stub_response(self, query: str) -> Tuple[List[str], str, DangerLevel]:
        """
        Stub responses for demonstration (Phase 1).
        
        Args:
            query: Natural language query
        
        Returns:
            Tuple of (commands, explanation, danger_level)
        """
        query_lower = query.lower()
        
        # Some hardcoded examples for demo
        if "port" in query_lower and "kill" in query_lower:
            # Extract port number if present
            import re
            port_match = re.search(r'\d{4,5}', query)
            port = port_match.group() if port_match else "3000"
            
            commands = [f"lsof -ti :{port} | xargs kill -9"]
            explanation = f"Kill process using port {port}"
            level = DangerLevel.DANGEROUS
            
        elif "node_modules" in query_lower and "clean" in query_lower:
            commands = ["rm -rf node_modules", "npm install"]
            explanation = "Remove node_modules and reinstall dependencies"
            level = DangerLevel.DANGEROUS
            
        elif "git" in query_lower and "status" in query_lower:
            commands = ["git status -s"]
            explanation = "Show git status"
            level = DangerLevel.SAFE
            
        elif "docker" in query_lower and "restart" in query_lower:
            commands = ["docker-compose down", "docker-compose up -d"]
            explanation = "Restart docker containers"
            level = DangerLevel.CAUTION
            
        else:
            commands = []
            explanation = "Phase 2 Feature: LLM integration coming soon!"
            level = DangerLevel.SAFE
        
        return commands, explanation, level
    
    def enable_llm(self, provider: str, api_key: str):
        """
        Enable LLM integration (Phase 2).
        
        Args:
            provider: "openai" or "anthropic"
            api_key: API key for the provider
        """
        # Phase 2: Initialize LLM client
        self.llm_enabled = True
        # TODO: Store provider and initialize client
    
    def generate_commands_from_nl(self, nl_description: str, context: Optional[dict] = None) -> List[str]:
        """
        Generate commands from natural language description (for NL macros).
        
        Args:
            nl_description: Natural language description of what to do
            context: Optional context information
        
        Returns:
            List of shell commands
        """
        # Phase 2: Use LLM to generate commands
        # For now, return placeholder
        return [f"# TODO: {nl_description}"]

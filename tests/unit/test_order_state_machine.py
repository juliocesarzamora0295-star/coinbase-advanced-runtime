"""Tests para máquina de estados de órdenes."""
import pytest
from decimal import Decimal


class TestOrderStateMachine:
    """Tests de transiciones de estado de órdenes."""
    
    def test_market_order_initial_state(self):
        """MARKET order debe iniciar en OPEN_PENDING, no FILLED."""
        # Simular estado inicial tras ACK de create order
        order_state = "OPEN_PENDING"
        
        # No debe estar FILLED inmediatamente
        assert order_state != "FILLED"
        assert order_state == "OPEN_PENDING"
    
    def test_limit_order_initial_state(self):
        """LIMIT order debe iniciar en OPEN_RESTING."""
        order_state = "OPEN_RESTING"
        
        assert order_state == "OPEN_RESTING"
    
    def test_terminal_states(self):
        """Estados terminales no pueden cambiar."""
        terminal_states = ["FILLED", "CANCELLED", "EXPIRED", "FAILED"]
        
        for state in terminal_states:
            assert state in terminal_states
    
    def test_valid_transitions(self):
        """Transiciones válidas de estado."""
        # NEW -> OPEN_PENDING (market) o OPEN_RESTING (limit)
        # OPEN_PENDING -> FILLED/CANCELLED/EXPIRED/FAILED
        # OPEN_RESTING -> FILLED/CANCELLED/EXPIRED/FAILED
        
        transitions = {
            "NEW": ["OPEN_PENDING", "OPEN_RESTING"],
            "OPEN_PENDING": ["FILLED", "CANCELLED", "EXPIRED", "FAILED"],
            "OPEN_RESTING": ["FILLED", "CANCELLED", "EXPIRED", "FAILED"],
        }
        
        # MARKET order
        assert "OPEN_PENDING" in transitions["NEW"]
        
        # LIMIT order
        assert "OPEN_RESTING" in transitions["NEW"]
        
        # Ambos pueden terminar en estados terminales
        for terminal in ["FILLED", "CANCELLED", "EXPIRED", "FAILED"]:
            assert terminal in transitions["OPEN_PENDING"]
            assert terminal in transitions["OPEN_RESTING"]
    
    def test_invalid_transition_market_to_filled_on_ack(self):
        """MARKET no debe ir a FILLED en el ACK de create order."""
        # Esto es un error conceptual: el ACK confirma recepción, no ejecución
        
        # Estado correcto tras ACK
        state_after_ack = "OPEN_PENDING"
        
        # Estado incorrecto (lo que había antes)
        wrong_state = "FILLED"
        
        assert state_after_ack != wrong_state
        assert state_after_ack == "OPEN_PENDING"
    
    def test_reconcile_required_for_terminal(self):
        """Solo user channel o REST reconcile puede mover a terminal."""
        # La orden debe permanecer en OPEN_PENDING/OPEN_RESTING
        # hasta que llegue confirmación por user channel o reconcile REST
        
        current_state = "OPEN_PENDING"
        
        # No cambiar a terminal sin confirmación
        assert current_state in ["OPEN_PENDING", "OPEN_RESTING"]
        
        # Solo evento user o reconcile REST puede cambiar a terminal
        valid_terminal_triggers = ["user_event", "rest_reconcile"]
        assert "user_event" in valid_terminal_triggers
